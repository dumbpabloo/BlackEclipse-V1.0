"""Stall watchdog for agent runs — every run reaches a terminal state.

Belt-and-suspenders beside the per-call hard deadlines (`async_timeout.py`): a
run row in `created`/`running` whose last recorded activity is older than its
own model budget + grace is FAILED with code `RUN_STALLED`, a durable
`run.failed` event is appended, the live SSE stream is closed and the active
task is cancelled. A client can therefore never wait forever, whatever strand
of the runtime misbehaves (a swallowed cancellation, a provider that stops
emitting after its deadline, a bug in a new subsystem).

Runs legitimately waiting on a USER DECISION (last event `approval.requested` /
`user.approval.required`) are exempt: their wait is bounded by the approval
timeout inside the run itself, not by model progress.

A run that already emitted a durable TERMINAL event (`run.completed` /
`run.failed` / `run.cancelled` / `run.interrupted`) is never "stalled": it
finished and only the row write lagged. That case is reconciled to the proven
status instead of overwritten — the exact bug that relabelled completed runs as
RUN_STALLED minutes later.

Pure `find_stalled` for tests + `sweep_stalled_runs` as the DB adapter.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import json
import logging
import time
from typing import Any, Callable, Mapping, Optional

from .agents import AGENT_PRESETS
from .persistence import new_id, now_iso

log = logging.getLogger("harness.watchdog")

DEFAULT_MODEL_BUDGET_S = 120.0
STALL_GRACE_S = 60.0
#: Fallback for the per-ONE-model-call ceiling (`step.MODEL_CALL_TIMEOUT_CEILING_S`)
#: when it cannot be imported. A healthy run can legitimately be silent for a
#: whole model call, and the gateway allows that call up to the ceiling — a
#: shorter watchdog budget would kill a healthy run mid-call (observed: a 218s
#: silent-but-live call was failed as RUN_STALLED).
DEFAULT_MODEL_SILENCE_S = 300.0
SWEEP_INTERVAL_S = 30.0
APPROVAL_EVENTS = frozenset({
    "approval.requested", "user.approval.required",
    # A `clarify` tool call parks the run on the same human-decision gate.
    "clarification.requested",
})

#: A durable terminal event proves the run reached a terminal state, whatever
#: the row says. If the row is still `running`, the status WRITE was lost (not
#: the run) and the watchdog must reconcile it instead of overwriting a finished
#: run with RUN_STALLED.
TERMINAL_EVENT_STATUS = {
    "run.completed": "completed",
    "run.failed": "failed",
    "run.cancelled": "cancelled",
    "run.interrupted": "interrupted",
}

_TERMINAL_TYPES = ", ".join(f"'{name}'" for name in TERMINAL_EVENT_STATUS)


def _terminal_where(alias: str) -> str:
    """SQL predicate for "a terminal event that proves the outcome".

    A watchdog `RUN_STALLED` event is excluded: it is the watchdog's own
    conclusion, so letting it count as proof would make the mislabelled history
    self-confirming (an earlier `run.completed` is the real terminal fact).
    """
    return (
        f"{alias}.type IN ({_TERMINAL_TYPES}) "
        f"AND NOT ({alias}.type = 'run.failed' AND {alias}.data LIKE '%\"RUN_STALLED\"%')"
    )


_STALLED_SQL = f"""
SELECT r.id, r.status, r.agentId,
       COALESCE(r.startedAt, r.createdAt) AS started,
       (SELECT MAX(e.at) FROM HarnessEvent e WHERE e.runId = r.id) AS lastEventAt,
       (SELECT e2.type FROM HarnessEvent e2 WHERE e2.runId = r.id ORDER BY e2.seq DESC LIMIT 1) AS lastEventType,
       (SELECT e3.type FROM HarnessEvent e3 WHERE e3.runId = r.id
          AND {_terminal_where("e3")} ORDER BY e3.seq DESC LIMIT 1) AS terminalType,
       (SELECT e4.at FROM HarnessEvent e4 WHERE e4.runId = r.id
          AND {_terminal_where("e4")} ORDER BY e4.seq DESC LIMIT 1) AS terminalAt,
       (SELECT e5.data FROM HarnessEvent e5 WHERE e5.runId = r.id
          AND {_terminal_where("e5")} ORDER BY e5.seq DESC LIMIT 1) AS terminalData
FROM HarnessRun r
WHERE r.status IN ('created', 'running')
"""


def _model_silence_ceiling_s() -> float:
    """The per-call ceiling one model call may consume (best-effort import)."""
    try:
        from .step import MODEL_CALL_TIMEOUT_CEILING_S  # noqa: PLC0415

        return float(MODEL_CALL_TIMEOUT_CEILING_S)
    except Exception:  # noqa: BLE001 - a missing module must not break the watch
        return DEFAULT_MODEL_SILENCE_S


def stall_threshold_s(agent_id: Optional[str]) -> float:
    """The silence budget for one run: one model call + grace.

    `runTimeoutMs` bounds ONE model call; a healthy run can be silent for at
    most that long (longer silences mean the deadline machinery itself failed).
    The effective ceiling is `max(agent budget, model-call ceiling)`: the gateway
    caps a call at the ceiling, so a run sitting in a 250s call is healthy even
    when the agent declares 120s. Eclipse Agent => 360s, Code Agent => 660s.
    """
    preset = AGENT_PRESETS.get(agent_id or "")
    budget_ms = getattr(preset, "runTimeoutMs", None) or int(DEFAULT_MODEL_BUDGET_S * 1000)
    return max(budget_ms / 1000.0, _model_silence_ceiling_s()) + STALL_GRACE_S


def _epoch(stamp: Optional[str]) -> Optional[float]:
    if not stamp:
        return None
    try:
        parsed = _dt.datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_dt.timezone.utc)
    return parsed.timestamp()


def terminal_event_of(row: Mapping[str, Any]) -> Optional[str]:
    """The run's terminal status proven by its event log, when one applies.

    `terminalAt` must be at/after `started`: a resume re-marks the row running
    with a new `startedAt`, so the earlier completion belongs to the previous
    attempt and must not finalize the current one.
    """
    event_type = row.get("terminalType")
    if event_type not in TERMINAL_EVENT_STATUS:
        return None
    at = _epoch(row.get("terminalAt"))
    started = _epoch(row.get("started"))
    if at is not None and started is not None and at < started:
        return None
    return TERMINAL_EVENT_STATUS[event_type]


def find_stalled(
    rows: list[dict[str, Any]],
    now_epoch: float,
    threshold_for: Callable[[Optional[str]], float] = stall_threshold_s,
    live_run_ids: Optional[set[str]] = None,
) -> list[str]:
    """The pure detector: run ids whose last activity exceeds their silence budget.

    A run with a live lease is skipped even when its event log is silent: another
    runtime is executing it, and failing it here would steal the run from its
    owner. `live_run_ids is None` preserves the old signature for callers that
    do not have lease information.

    A run that already has a durable terminal event is NOT stalled — it finished,
    and only its row write was lost. `reconcile_terminal_runs` repairs those.
    """
    stalled: list[str] = []
    for row in rows:
        run_id = str(row.get("id"))
        if live_run_ids is not None and run_id in live_run_ids:
            continue
        if row.get("lastEventType") in APPROVAL_EVENTS:
            continue  # waiting on the user; bounded by the approval timeout
        if terminal_event_of(row) is not None:
            continue  # finished; the row status lagged the event
        last = _epoch(row.get("lastEventAt")) or _epoch(row.get("started"))
        if last is None:
            continue
        if now_epoch - last > threshold_for(row.get("agentId")):
            stalled.append(run_id)
    return stalled


def _terminal_error(row: Mapping[str, Any]) -> Optional[dict[str, Any]]:
    """The error payload of a terminal FAILURE event, normalized.

    A success event carries no error; its data is telemetry and must not be
    stored as one.
    """
    if row.get("terminalType") != "run.failed":
        return None
    raw = row.get("terminalData")
    if not raw:
        return None
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    nested = data.get("error")
    if isinstance(nested, dict):
        return nested
    return data or None


#: Non-terminal rows plus rows this watchdog (and only this watchdog) previously
#: relabelled RUN_STALLED. The latter is the self-healing half: before the write
#: fix, a finished run was marked RUN_STALLED minutes later, and that history is
#: still wrong in the row.
_RECONCILE_SQL = f"""
SELECT r.id, r.status, r.agentId, r.error,
       COALESCE(r.startedAt, r.createdAt) AS started,
       (SELECT e3.type FROM HarnessEvent e3 WHERE e3.runId = r.id
          AND {_terminal_where("e3")} ORDER BY e3.seq DESC LIMIT 1) AS terminalType,
       (SELECT e4.at FROM HarnessEvent e4 WHERE e4.runId = r.id
          AND {_terminal_where("e4")} ORDER BY e4.seq DESC LIMIT 1) AS terminalAt,
       (SELECT e5.data FROM HarnessEvent e5 WHERE e5.runId = r.id
          AND {_terminal_where("e5")} ORDER BY e5.seq DESC LIMIT 1) AS terminalData
FROM HarnessRun r
WHERE r.status IN ('created', 'running')
   OR (r.status = 'failed' AND r.error LIKE '%RUN_STALLED%')
"""


def _proven_status(row: Mapping[str, Any]) -> Optional[str]:
    """The status this row's own event log proves, or None when it agrees."""
    proven = terminal_event_of(row)
    if proven is None:
        return None
    current = str(row.get("status") or "")
    if current == proven:
        return None
    if current in ("created", "running"):
        return proven
    # A row the watchdog itself failed while a completion was durable is the
    # known mislabel. Any other failed status is a real failure: leave it.
    if current == "failed" and proven == "completed" and "RUN_STALLED" in str(row.get("error") or ""):
        return proven
    return None


def reconcile_terminal_runs(
    store: Any, db: Any, rows: list[dict[str, Any]], live_run_ids: Optional[set[str]],
) -> list[str]:
    """Repair rows whose terminal event landed but whose status write was lost.

    Only runs without a live owner are touched: a live lease means the owner may
    still be finishing its own terminal write. Idempotent — the conditional
    UPDATE makes a second sweep a no-op.
    """
    repaired: list[str] = []
    for row in rows:
        run_id = str(row.get("id"))
        if live_run_ids is not None and run_id in live_run_ids:
            continue
        status = _proven_status(row)
        if status is None:
            continue
        try:
            partial = _last_assistant_text(db, run_id)
            error = _terminal_error(row)
            now = now_iso()
            if status == "completed":
                # Clear the RUN_STALLED error the old watchdog wrote over a run
                # that had already answered, and prefer the fuller durable
                # answer over the short "partial" the watchdog stored.
                cur = db.execute(
                    "UPDATE HarnessRun SET status = ?, updatedAt = ?, error = NULL, "
                    "finishedAt = COALESCE(?, ?), "
                    "output = CASE WHEN ? IS NOT NULL AND length(?) > COALESCE(length(output), 0) "
                    "THEN ? ELSE output END "
                    "WHERE id = ? AND status = ?",
                    (status, now, row.get("terminalAt"), now,
                     partial, partial, partial, run_id, str(row.get("status") or "")),
                )
            else:
                cur = db.execute(
                    "UPDATE HarnessRun SET status = ?, output = COALESCE(output, ?), "
                    "error = COALESCE(?, error), finishedAt = COALESCE(?, ?), updatedAt = ? "
                    "WHERE id = ? AND status = ?",
                    (status, partial, json.dumps(error) if error else None,
                     row.get("terminalAt"), now, now,
                     run_id, str(row.get("status") or "")),
                )
            if not cur.rowcount:
                continue
            db.commit()
            repaired.append(run_id)
            log.warning(
                "reconciled run %s to %s from its terminal event (status write was lost)",
                run_id, status,
            )
        except Exception:  # noqa: BLE001 - one bad row must not stop the sweep
            log.warning("watchdog failed to reconcile run %s", run_id, exc_info=True)
    return repaired


def _last_assistant_text(db: Any, run_id: str) -> Optional[str]:
    """The most recent assistant text, reconstructed from the durable message log.

    A stalled run has already produced text that the caller never received; the
    watchdog must not discard it. LangChain message payloads carry either a
    string content or a list of content blocks.
    """
    try:
        rows = db.execute(
            "SELECT payload FROM RunMessage WHERE runId=? ORDER BY seq DESC LIMIT 40",
            (run_id,),
        ).fetchall()
    except Exception:  # noqa: BLE001 - no table means no partial
        return None
    for row in rows:
        try:
            payload = json.loads(row["payload"])
        except (TypeError, ValueError):
            continue
        if not isinstance(payload, dict) or payload.get("type") not in ("ai", "AIMessage"):
            continue
        content = (payload.get("data") or {}).get("content")
        if isinstance(content, str) and content.strip():
            return content.strip()
        if isinstance(content, list):
            parts: list[str] = []
            for block in content:
                if isinstance(block, str):
                    parts.append(block)
                elif isinstance(block, dict) and isinstance(block.get("text"), str):
                    parts.append(block["text"])
            text = "\n".join(parts).strip()
            if text:
                return text
    return None


def _append_event(store: Any, db: Any, run_id: str, event_type: str, data: Any) -> None:
    """Append a durable event through the W6 allocator (legacy fallback)."""
    try:
        from .event_sequence import get_event_sequence  # noqa: PLC0415

        sequence = get_event_sequence(store)
        if sequence is not None:
            sequence.append(run_id, event_type, data if isinstance(data, dict) else json.loads(data))
            return
    except Exception:  # noqa: BLE001 - fall through to the legacy insert
        log.warning("sequenced watchdog event failed", exc_info=True)
    max_seq = db.execute(
        "SELECT COALESCE(MAX(seq), 0) AS s FROM HarnessEvent WHERE runId = ?", (run_id,)
    ).fetchone()["s"]
    db.execute(
        "INSERT INTO HarnessEvent (id, runId, type, seq, at, data) VALUES (?, ?, ?, ?, ?, ?)",
        (new_id("evt"), run_id, event_type, max_seq + 1, now_iso(),
         data if isinstance(data, str) else json.dumps(data)),
    )
    db.commit()


def sweep_stalled_runs(service: Any, now_epoch: Optional[float] = None) -> list[str]:
    """Fail every currently stalled run; returns the run ids. Never raises."""
    try:
        db = service.store._db  # noqa: SLF001 - the one shared connection
        rows = [dict(r) for r in db.execute(_STALLED_SQL).fetchall()]
        reconcile_rows = [dict(r) for r in db.execute(_RECONCILE_SQL).fetchall()]
        now_ms = int((now_epoch if now_epoch is not None else time.time()) * 1000)
        try:
            live = {
                str(r["runId"]) for r in db.execute(
                    "SELECT runId FROM RunLease WHERE expiresAtMs >= ?", (now_ms,)
                ).fetchall()
            }
        except Exception:  # noqa: BLE001 - no lease table means no live owner
            live = set()
        # A run that is emitting token deltas is alive even though deltas are
        # transient and never appear in `HarnessEvent`. Without this, a healthy
        # long generation was eligible for RUN_STALLED as soon as the last
        # durable event aged past the budget.
        from .run_activity import active_run_ids, prune as prune_activity  # noqa: PLC0415

        live |= active_run_ids()
        prune_activity(older_than_s=3600.0)
        # Repair first: a run that already emitted a terminal event finished, so
        # the row is merely stale. Doing this before the stall check keeps a
        # finished run from ever being relabelled RUN_STALLED.
        reconcile_terminal_runs(service.store, db, reconcile_rows, live)
        stalled = find_stalled(
            rows, now_epoch if now_epoch is not None else time.time(), live_run_ids=live,
        )
    except Exception:  # noqa: BLE001 - the watch must never break the runtime
        log.warning("run watchdog sweep failed", exc_info=True)
        return []
    for run_id in stalled:
        try:
            now = now_iso()
            partial = _last_assistant_text(db, run_id)
            error_payload = {
                "code": "RUN_STALLED",
                "message": "no progress within this run's model budget; marked failed so the stream closes",
            }
            if partial:
                error_payload["partial"] = True
                error_payload["partialChars"] = len(partial)
            error = json.dumps(error_payload)
            cur = db.execute(
                "UPDATE HarnessRun SET status = 'failed', output = COALESCE(output, ?), "
                "error = ?, updatedAt = ?, finishedAt = ? "
                "WHERE id = ? AND status IN ('created', 'running')",
                (partial, error, now, now, run_id),
            )
            if not cur.rowcount:
                continue  # finished between the scan and the update
            db.commit()
            _append_event(service.store, db, run_id, "run.failed", error)
            if partial:
                _append_event(
                    service.store, db, run_id, "run.partial",
                    json.dumps({"chars": len(partial), "reason": "stalled"}),
                )
            service.bus.end_stream(run_id)
            service.cancel(run_id)  # cooperative; the hard deadlines do the rest
            log.warning("watchdog failed stalled run %s", run_id)
        except Exception:  # noqa: BLE001 - one bad row must not stop the sweep
            log.warning("watchdog failed to fail run %s", run_id, exc_info=True)
    return stalled


_watchdog_task: Optional["asyncio.Task[None]"] = None


async def start_run_watchdog(service: Any, interval_s: float = SWEEP_INTERVAL_S) -> None:
    """Start the periodic sweep (idempotent). Best-effort; never blocks boot."""
    global _watchdog_task
    if _watchdog_task is not None and not _watchdog_task.done():
        return

    async def _loop() -> None:
        while True:
            # Runs ON the event loop: `service.bus.end_stream` / `service.cancel`
            # touch asyncio primitives, which must stay on this thread. The query
            # itself is short (non-terminal runs only).
            sweep_stalled_runs(service)
            await asyncio.sleep(interval_s)

    _watchdog_task = asyncio.create_task(_loop())


async def stop_run_watchdog() -> None:
    """Stop the sweep (idempotent, never raises)."""
    global _watchdog_task
    task = _watchdog_task
    _watchdog_task = None
    if task is None or task.done():
        return
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):  # noqa: BLE001
        pass


__all__ = [
    "APPROVAL_EVENTS",
    "STALL_GRACE_S",
    "SWEEP_INTERVAL_S",
    "TERMINAL_EVENT_STATUS",
    "find_stalled",
    "reconcile_terminal_runs",
    "stall_threshold_s",
    "start_run_watchdog",
    "stop_run_watchdog",
    "sweep_stalled_runs",
    "terminal_event_of",
]
