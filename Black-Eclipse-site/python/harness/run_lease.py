"""Run leases: one live owner per run, with a fencing epoch.

The runtime executes runs in-process. Before this module, a second process could
reconcile a run a live process was executing, and two recovery attempts could
resume the same run. The lease makes ownership explicit and durable:

  * `RunLease`        — one row per run: owner runtime, epoch, expiry, heartbeat.
  * `RuntimeInstance` — one row per runtime process: environment, port, DB, pid.

Rules:
  * a claim is atomic (`BEGIN IMMEDIATE` + conditional UPDATE/INSERT); a live
    lease cannot be stolen;
  * every successful claim increments `epoch` — the fencing token a writer can
    use to prove it still owns the run;
  * the heartbeat is a DAEMON THREAD with its own SQLite connection (the agent
    event loop can be blocked by a synchronous tool; the lease must not stall);
  * an expired lease is reclaimable; recovery must skip a live one.

This module owns only its own tables/columns. It never touches `jdds.db`.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time
import uuid
from typing import Any, Optional

from .persistence import now_iso
from .run_patch import run_patch_sql

log = logging.getLogger("harness.run_lease")

LEASE_TTL_MS = 60_000
HEARTBEAT_INTERVAL_S = 10.0
SCHEMA = """
CREATE TABLE IF NOT EXISTS RunLease (
  runId        TEXT PRIMARY KEY,
  environment  TEXT NOT NULL,
  runtimeId    TEXT NOT NULL,
  epoch        INTEGER NOT NULL,
  acquiredAt   TEXT NOT NULL,
  heartbeatAt  TEXT NOT NULL,
  expiresAtMs  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_run_lease_expiry ON RunLease(expiresAtMs);

CREATE TABLE IF NOT EXISTS RuntimeInstance (
  runtimeId    TEXT PRIMARY KEY,
  environment  TEXT NOT NULL,
  port         INTEGER,
  dbPath       TEXT NOT NULL,
  pid          INTEGER,
  startedAt    TEXT NOT NULL,
  heartbeatAt  TEXT NOT NULL,
  expiresAtMs  INTEGER NOT NULL
);
"""


def _environment() -> str:
    return (os.environ.get("BE_PROFILE") or "prod").strip() or "prod"


def _now_ms() -> int:
    return int(time.time() * 1000)


_RUNTIME_ID: Optional[str] = None
_PROCESS_STARTED_AT = now_iso()


def default_runtime_id() -> str:
    """One identity per PROCESS (not per run): all lease managers share it."""
    global _RUNTIME_ID
    if _RUNTIME_ID is None:
        port = os.environ.get("HARNESS_PY_PORT") or "0"
        _RUNTIME_ID = f"{_environment()}:{os.getpid()}:{port}:{uuid.uuid4().hex[:8]}"
    return _RUNTIME_ID


class RunLeaseManager:
    """Lease + runtime-identity operations over the ONE agent-memory store."""

    def __init__(
        self,
        store: Any,
        *,
        runtime_id: Optional[str] = None,
        environment: Optional[str] = None,
        ttl_ms: int = LEASE_TTL_MS,
    ) -> None:
        self._store = store
        self.runtime_id = runtime_id or default_runtime_id()
        self.environment = environment or _environment()
        self.ttl_ms = max(5_000, int(ttl_ms))
        self._ensure_schema()
        self.register_runtime()

    @property
    def _db(self):
        return self._store._db

    def _ensure_schema(self) -> None:
        self._db.executescript(SCHEMA)
        existing = {row["name"] for row in self._db.execute("PRAGMA table_info(HarnessRun)").fetchall()}
        for name, decl in (
            ("environment", "TEXT"),
            ("runtimeId", "TEXT"),
            ("leaseEpoch", "INTEGER"),
        ):
            if name not in existing:
                self._db.execute(f"ALTER TABLE HarnessRun ADD COLUMN {name} {decl}")
        self._db.commit()

    # ── runtime identity ─────────────────────────────────────────────────────
    def register_runtime(self, port: Optional[int] = None) -> list[str]:
        """Record this runtime; return other fresh runtimes on the same DB.

        A conflict is advisory (dev/prod share `jdds.db` on purpose; a
        misconfigured second prod runtime is a real risk). The caller logs it.
        """
        try:
            port = int(port if port is not None else (os.environ.get("HARNESS_PY_PORT") or 0))
        except (TypeError, ValueError):
            port = 0
        db_path = str(getattr(self._store, "path", "unknown"))
        self._db.execute(
            "INSERT OR REPLACE INTO RuntimeInstance "
            "(runtimeId, environment, port, dbPath, pid, startedAt, heartbeatAt, expiresAtMs) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (self.runtime_id, self.environment, port, db_path, os.getpid(),
             _PROCESS_STARTED_AT, now_iso(), _now_ms() + self.ttl_ms),
        )
        self._db.commit()
        try:
            rows = self._db.execute(
                "SELECT runtimeId, environment FROM RuntimeInstance "
                "WHERE dbPath=? AND expiresAtMs>=? AND runtimeId<>?",
                (db_path, _now_ms(), self.runtime_id),
            ).fetchall()
        except Exception:  # noqa: BLE001 - identity is advisory
            return []
        return [f"{r['runtimeId']} ({r['environment']})" for r in rows]

    def touch_runtime(self) -> None:
        try:
            self._db.execute(
                "UPDATE RuntimeInstance SET heartbeatAt=?, expiresAtMs=? WHERE runtimeId=?",
                (now_iso(), _now_ms() + self.ttl_ms, self.runtime_id),
            )
            self._db.commit()
        except Exception:  # noqa: BLE001 - best effort
            pass

    # ── lease ────────────────────────────────────────────────────────────────
    def claim(self, run_id: str) -> Optional[int]:
        """Atomically claim `run_id`; return the new epoch, or None if held."""
        if not run_id:
            return None
        now = _now_ms()
        expires = now + self.ttl_ms
        self._db.execute("BEGIN IMMEDIATE")
        try:
            cur = self._db.execute(
                "UPDATE RunLease SET environment=?, runtimeId=?, epoch=epoch+1, "
                "acquiredAt=?, heartbeatAt=?, expiresAtMs=? "
                "WHERE runId=? AND expiresAtMs<?",
                (self.environment, self.runtime_id, now_iso(), now_iso(), expires,
                 run_id, now),
            )
            if cur.rowcount == 0:
                row = self._db.execute(
                    "SELECT runtimeId, expiresAtMs FROM RunLease WHERE runId=?", (run_id,)
                ).fetchone()
                if row is not None:
                    self._db.execute("COMMIT")
                    return None  # a live owner holds it
                self._db.execute(
                    "INSERT INTO RunLease "
                    "(runId, environment, runtimeId, epoch, acquiredAt, heartbeatAt, expiresAtMs) "
                    "VALUES (?, ?, ?, 1, ?, ?, ?)",
                    (run_id, self.environment, self.runtime_id, now_iso(), now_iso(), expires),
                )
            epoch_row = self._db.execute(
                "SELECT epoch FROM RunLease WHERE runId=?", (run_id,)
            ).fetchone()
            epoch = int(epoch_row["epoch"]) if epoch_row else 1
            self._db.execute(
                "UPDATE HarnessRun SET environment=?, runtimeId=?, leaseEpoch=? WHERE id=?",
                (self.environment, self.runtime_id, epoch, run_id),
            )
            self._db.execute("COMMIT")
            return epoch
        except Exception:
            self._db.execute("ROLLBACK")
            raise

    def heartbeat_result(self, run_id: str, epoch: int) -> str:
        """Extend the lease; classify the outcome for the heartbeat thread.

        Returns ``"ok"`` when the lease was extended, ``"fenced"`` when another
        runtime owns this run/epoch (the worker must stop), and ``"error"`` for
        a transient failure (a locked/busy database). A transient error must
        NOT kill the heartbeat thread: the old code returned `False` for any
        exception, so one >5 s write lock silently stopped the heartbeat, the
        lease expired, and the run was fenced mid-chat (observed 2026-11-26).
        """
        try:
            cur = self._db.execute(
                "UPDATE RunLease SET heartbeatAt=?, expiresAtMs=? "
                "WHERE runId=? AND runtimeId=? AND epoch=?",
                (now_iso(), _now_ms() + self.ttl_ms, run_id, self.runtime_id, int(epoch)),
            )
            self._db.commit()
            return "ok" if cur.rowcount == 1 else "fenced"
        except Exception:  # noqa: BLE001 - retried on the next beat
            log.warning("lease heartbeat failed for %s; will retry", run_id, exc_info=True)
            try:
                self._db.rollback()
            except Exception:  # noqa: BLE001
                pass
            return "error"

    def heartbeat(self, run_id: str, epoch: int) -> bool:
        """True while this process still owns the epoch (transient errors retry)."""
        return self.heartbeat_result(run_id, epoch) != "fenced"

    def release(self, run_id: str, epoch: int) -> None:
        """Give the lease up immediately (terminal run)."""
        try:
            self._db.execute(
                "UPDATE RunLease SET expiresAtMs=? "
                "WHERE runId=? AND runtimeId=? AND epoch=?",
                (_now_ms() - 1, run_id, self.runtime_id, int(epoch)),
            )
            self._db.commit()
        except Exception:  # noqa: BLE001
            pass

    def is_live(self, run_id: str) -> bool:
        try:
            row = self._db.execute(
                "SELECT expiresAtMs FROM RunLease WHERE runId=?", (run_id,)
            ).fetchone()
        except Exception:  # noqa: BLE001 - no table means no live lease
            return False
        return bool(row and int(row["expiresAtMs"]) >= _now_ms())

    def owner(self, run_id: str) -> Optional[dict[str, Any]]:
        try:
            row = self._db.execute(
                "SELECT * FROM RunLease WHERE runId=?", (run_id,)
            ).fetchone()
        except Exception:  # noqa: BLE001
            return None
        return dict(row) if row else None


# The patch allowlist and the JSON encoding are owned by `persistence` so this
# writer and `AgentMemoryStore.update_run` cannot drift apart again.


def fenced_update_run(store: Any, run_id: str, *, runtime_id: Optional[str] = None,
                      **patch: Any) -> bool:
    """Write a run field ONLY if this process still owns the lease epoch.

    A stale worker that lost its lease must not be able to write a terminal
    status over the new owner's run. Runs created before leasing (or executed
    directly in tests without a lease) have `leaseEpoch IS NULL` and are allowed.
    Returns False when the write was fenced (no row changed).
    """
    if not hasattr(store, "_db"):
        store.update_run(run_id, **patch)
        return True
    db = store._db
    try:
        columns = {row["name"] for row in db.execute("PRAGMA table_info(HarnessRun)").fetchall()}
    except Exception:  # noqa: BLE001 - an unusual store: fall back
        store.update_run(run_id, **patch)
        return True
    if "leaseEpoch" not in columns:
        store.update_run(run_id, **patch)
        return True

    sets, params = run_patch_sql(patch)
    if not sets:
        return False
    sql = (
        f"UPDATE HarnessRun SET {', '.join(sets)} WHERE id = ? AND ("
        "leaseEpoch IS NULL OR leaseEpoch = ("
        "SELECT epoch FROM RunLease WHERE runId = ? AND runtimeId = ? AND expiresAtMs >= ?))"
    )
    params.extend([run_id, run_id, runtime_id or default_runtime_id(), _now_ms()])
    # A transient write lock gets one retry. Anything else is a real defect and
    # must be LOUD: silently returning False once left every run non-terminal,
    # so the stall watchdog overwrote completed runs with RUN_STALLED.
    for attempt in (1, 2):
        try:
            cur = db.execute(sql, params)
            db.commit()
            return cur.rowcount > 0
        except sqlite3.OperationalError:  # locked/busy: worth one retry
            if attempt == 1:
                continue
            log.error("fenced update hit a persistent lock for %s", run_id, exc_info=True)
            return False
        except Exception:  # noqa: BLE001 - fencing must not crash the run path
            log.error(
                "fenced update failed for %s (fields=%s); the run status may be stale",
                run_id, sorted(patch), exc_info=True,
            )
            return False
    return False


class LeaseLost(RuntimeError):
    """This process no longer owns the run's lease epoch.

    A fenced write returned zero rows: another runtime claimed the run, the
    lease expired, or the run row disappeared. The caller must STOP writing and
    let the new owner (or recovery) reconcile the run.
    """

    code = "RUN_LEASE_LOST"


def fenced_progress(store: Any, run_id: str, *, runtime_id: Optional[str] = None,
                    **patch: Any) -> None:
    """Write run progress iff this runtime still owns the lease epoch.

    This is the W6 entry point for every non-terminal run mutation. It honours
    `HARNESS_FENCED_PROGRESS`; when the flag is off the legacy `update_run` is
    used so an operator can roll back without a code change.

    Raises `LeaseLost` when the write is fenced. Callers must not ignore it: a
    stale worker that keeps writing can clobber the live owner's counters and
    terminal status.
    """
    from .config.flags import get_bool  # noqa: PLC0415 - flags must stay importable

    if not get_bool("HARNESS_FENCED_PROGRESS"):
        store.update_run(run_id, **patch)
        return
    if not fenced_update_run(store, run_id, runtime_id=runtime_id, **patch):
        raise LeaseLost(
            f"run {run_id!r} is no longer owned by this runtime (lease fenced)"
        )


class LeaseHeartbeat:
    """Daemon heartbeat for one lease; owns its own thread-local DB connection."""

    def __init__(self, manager: RunLeaseManager, run_id: str, epoch: int,
                 interval_s: float = HEARTBEAT_INTERVAL_S) -> None:
        self._manager = manager
        self._run_id = run_id
        self._epoch = int(epoch)
        self._interval = max(1.0, float(interval_s))
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._loop, name=f"lease-{run_id[-8:]}", daemon=True,
        )

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=2.0)

    def _loop(self) -> None:
        while not self._stop.wait(self._interval):
            outcome = self._manager.heartbeat_result(self._run_id, self._epoch)
            if outcome == "fenced":
                log.warning("lease lost for run %s (epoch %s)", self._run_id, self._epoch)
                return
            # outcome == "error": a transient lock/DB failure. Keep beating; the
            # next beat extends the lease. Returning here used to orphan the run.


_MANAGER_CACHE: dict[int, RunLeaseManager] = {}


def _manager_for(store: Any, **kwargs: Any) -> RunLeaseManager:
    """ONE manager per store per process (schema + runtime identity once).

    `RunLeaseGuard` is constructed for every run. Building a manager per run ran
    `executescript(SCHEMA)`, the `PRAGMA table_info` + `ALTER` checks and an
    `INSERT OR REPLACE` on RuntimeInstance on every chat start — extra write
    locks on the same multi-GB database the run is using.
    """
    key = id(store)
    manager = _MANAGER_CACHE.get(key)
    if kwargs:
        # An explicit identity/ttl (tests, embedded callers) is honoured exactly.
        return RunLeaseManager(store, **kwargs)
    if manager is None or manager._store is not store:  # noqa: SLF001 - identity cache
        manager = RunLeaseManager(store)
        _MANAGER_CACHE[key] = manager
    return manager


class RunLeaseGuard:
    """Claim a run, heartbeat it, release it on exit.

    `owned` is False when another live runtime holds the run; callers must not
    execute in that case (a duplicate submission, not a retry).
    """

    def __init__(self, store: Any, run_id: str, **kwargs: Any) -> None:
        # A non-SQLite/embedding store has no lease table. Leasing is a safety
        # layer, not a prerequisite for execution: degrade to "owned" rather
        # than breaking the run (tests and embedded callers use stubs).
        # ONE manager per store/process: constructing one per run re-ran the
        # schema DDL + ALTER checks and rewrote RuntimeInstance on EVERY chat,
        # adding write-lock contention to a multi-GB database.
        self.manager = _manager_for(store, **kwargs) if hasattr(store, "_db") else None
        self.run_id = run_id
        self.epoch: Optional[int] = None
        self._heartbeat: Optional[LeaseHeartbeat] = None

    @property
    def owned(self) -> bool:
        return self.manager is None or self.epoch is not None

    def __enter__(self) -> "RunLeaseGuard":
        if self.manager is None:
            return self
        self.epoch = self.manager.claim(self.run_id)
        if self.epoch is not None:
            self._heartbeat = LeaseHeartbeat(self.manager, self.run_id, self.epoch)
            self._heartbeat.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:  # noqa: ANN001
        if self._heartbeat is not None:
            self._heartbeat.stop()
        if self.manager is not None and self.epoch is not None:
            self.manager.release(self.run_id, self.epoch)


__all__ = [
    "LEASE_TTL_MS",
    "HEARTBEAT_INTERVAL_S",
    "LeaseHeartbeat",
    "LeaseLost",
    "RunLeaseGuard",
    "RunLeaseManager",
    "default_runtime_id",
    "fenced_progress",
    "fenced_update_run",
]
