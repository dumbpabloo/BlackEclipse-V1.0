"""Terminal settlement for runs whose worker disappeared.

A run's lease is its liveness contract. When the owning worker stops beating —
because the process was killed, replaced, or the task was cancelled outside
`execute_run` — the row can be left `running` with no terminal event. Before this
module the only thing that noticed was the stall watchdog, which waits for the
model silence budget (6+ minutes for the Eclipse Agent). The user sees a frozen
chat for that entire window.

Two seams close that gap:

  * `settle_lost_run` is called when a worker discovers it was fenced. It writes
    the terminal row ONLY when no *other* live runtime owns the run, so a stale
    worker can never overwrite the new owner.
  * `guard_background_task` wraps the background run task. If the task raises or
    is cancelled before `execute_run` settled the row, it writes an honest
    terminal state and a durable terminal event, then closes live streams.

Both are fail-open: settlement is a safety net, never a reason to fail a run.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from typing import Any, Optional

from .persistence import now_iso

log = logging.getLogger("harness.run_supervision")

_UNSETTLED = ("created", "running")


def _run_status(store: Any, run_id: str) -> Optional[str]:
    try:
        row = store._db.execute(  # noqa: SLF001 - the shared connection
            "SELECT status FROM HarnessRun WHERE id=?", (run_id,)
        ).fetchone()
    except Exception:  # noqa: BLE001 - an unreadable row is not ours to settle
        return None
    return str(row["status"]) if row is not None else None


def _other_live_owner(store: Any, run_id: str, runtime_id: Optional[str]) -> bool:
    """True when a DIFFERENT runtime still holds an unexpired lease."""
    try:
        row = store._db.execute(  # noqa: SLF001
            "SELECT runtimeId, expiresAtMs FROM RunLease WHERE runId=?", (run_id,)
        ).fetchone()
    except Exception:  # noqa: BLE001
        return False
    if row is None:
        return False
    if int(row["expiresAtMs"] or 0) < int(time.time() * 1000):
        return False
    if not runtime_id:
        # Unknown caller identity: a live lease is presumed to be someone else's.
        return True
    return str(row["runtimeId"]) != str(runtime_id)


def settle_unfinished_run(
    store: Any,
    run_id: str,
    *,
    status: str,
    code: str,
    message: str,
    runtime_id: Optional[str] = None,
) -> bool:
    """Write a terminal row for a run that is still `created`/`running`.

    Never overwrites a row a live owner (or `execute_run`) already settled, and
    never steals a run another live runtime owns. Returns True when it wrote.
    """
    if status not in ("failed", "cancelled", "interrupted"):
        raise ValueError(f"invalid settlement status: {status}")
    if _other_live_owner(store, run_id, runtime_id):
        return False
    error = {"code": code, "message": message}
    try:
        db = store._db  # noqa: SLF001
        cur = db.execute(
            "UPDATE HarnessRun SET status=?, error=?, updatedAt=?, finishedAt=? "
            "WHERE id=? AND status IN ('created','running')",
            (status, _json(error), now_iso(), now_iso(), run_id),
        )
        db.commit()
        return cur.rowcount > 0
    except sqlite3.OperationalError:
        log.warning("could not settle unfinished run %s (db busy)", run_id)
        return False
    except Exception:  # noqa: BLE001 - settlement must never crash the caller
        log.warning("could not settle unfinished run %s", run_id, exc_info=True)
        return False


def _json(value: Any) -> str:
    import json  # noqa: PLC0415 - tiny helper, keep the module import light

    return json.dumps(value)


async def supervise_background_run(work: Any, service: Any, run_id: str) -> None:
    """`guard_background_task` bound to the process-wide service/store.

    One call site in `service.run_background`, so the run manager keeps its own
    registry/lease wiring while this module owns the terminal fallback.
    """
    from .run_lease import default_runtime_id  # noqa: PLC0415

    await guard_background_task(
        work,
        getattr(service, "store", None),
        getattr(service, "bus", None),
        run_id,
        runtime_id=default_runtime_id(),
    )


async def settle_lost_run(
    store: Any,
    run_id: str,
    runtime_id: Optional[str],
    emitter: Any,
) -> bool:
    """Settle a run this worker lost, and tell its live stream why."""
    wrote = settle_unfinished_run(
        store,
        run_id,
        status="failed",
        code="RUN_LEASE_LOST",
        message=(
            "the agent runtime lost ownership of this run (the process restarted "
            "or another runtime took over); the work was stopped"
        ),
        runtime_id=runtime_id,
    )
    if not wrote:
        return False
    try:
        await emitter.emit("run.failed", {
            "error": {
                "code": "RUN_LEASE_LOST",
                "message": "the agent runtime lost ownership of this run",
            }
        })
    except Exception:  # noqa: BLE001 - the terminal row is the durable fact
        log.warning("could not emit run.failed for %s", run_id, exc_info=True)
    return True


async def guard_background_task(
    work: Any,
    store: Any,
    bus: Any,
    run_id: str,
    *,
    runtime_id: Optional[str] = None,
) -> None:
    """Run a background task and guarantee a terminal state if it escapes.

    `execute_run` owns the normal terminal write. This wrapper only fires when
    the body raises/cancels before that happened, so a crashed or killed task
    never leaves a row `running` (and never leaves a client stream hanging).
    """
    from .events import RunEmitter  # noqa: PLC0415 - avoid an import cycle

    try:
        await work
    except asyncio.CancelledError:
        kind, code = "cancelled", "RUN_CANCELLED"
        message = "the run was cancelled"
    except Exception as exc:  # noqa: BLE001 - last-resort terminal write
        kind, code = "failed", "RUN_CRASHED"
        message = f"the run stopped unexpectedly: {exc}"
        log.error("background run %s crashed without a terminal state", run_id, exc_info=True)
    else:
        return
    settle_unfinished_run(
        store, run_id, status=kind, code=code, message=message, runtime_id=runtime_id,
    )
    if bus is not None:
        try:
            await RunEmitter(run_id, store, bus).emit(
                "run.cancelled" if kind == "cancelled" else "run.failed",
                {"error": {"code": code, "message": message}}
                if kind != "cancelled" else {"reason": "cancelled"},
            )
        except Exception:  # noqa: BLE001 - the terminal row is the durable fact
            log.warning("could not emit terminal event for %s", run_id, exc_info=True)
        try:
            bus.end_stream(run_id)
        except Exception:  # noqa: BLE001
            pass
    if kind == "cancelled":
        # Re-raise after settling so asyncio still sees the cancellation; the
        # done callback pops the registry entry either way.
        raise asyncio.CancelledError()


__all__ = [
    "guard_background_task",
    "settle_lost_run",
    "settle_unfinished_run",
]
