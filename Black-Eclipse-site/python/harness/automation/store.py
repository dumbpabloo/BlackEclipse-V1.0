"""Automation persistence, bound to the ONE agent-memory connection.

Owns the Automation* tables and their helpers. Mirrors `jobs/store.py`,
`run_lineage.py` and `mcp/approvals.py`: bound to the single `AgentMemoryStore`
sqlite connection, owning only its own tables, so `persistence.py` — pinned at
its size ratchet — does not grow.

Durability rules:

  * DDL is idempotent (`CREATE TABLE IF NOT EXISTS`) + additive `ALTER TABLE`.
  * `nextRunAt` is indexed with `state` so due lookup never scans the table.
  * `AutomationEventSubscription` is the event-routing index: an event type maps
    to its subscriber automations, so an event never evaluates every automation.
  * Deleting an automation RETAINS its execution ledger (audit).
  * `migrate_legacy_jobs()` imports existing `HarnessJob` rows into `Automation`
    preserving ids and run history — never destroying data.
"""

from __future__ import annotations

import json
import uuid
from typing import Any, Optional

from ..persistence import now_iso
from .model import AutomationDefinition

TERMINAL_STATES = ("completed", "failed", "cancelled", "timed_out", "skipped", "blocked")


def new_automation_id() -> str:
    return "auto_" + uuid.uuid4().hex[:20]


def new_execution_id() -> str:
    return "autoexec_" + uuid.uuid4().hex[:20]


def _def_row(row: Any) -> AutomationDefinition:
    # The JSON blob is the immutable definition; the queryable columns are the
    # MUTABLE state (set by record_outcome / set_next_run / set_state). Overlay
    # the columns so a caller always sees the current state, never a stale blob.
    data = json.loads(row["definitionJson"])
    data["state"] = row["state"]
    data["version"] = row["version"]
    data["next_run_at"] = row["nextRunAt"]
    data["last_run_at"] = row["lastRunAt"]
    data["consecutive_failures"] = row["consecutiveFailures"]
    data["breaker"] = row["breaker"]
    data["breaker_opened_at"] = row["breakerOpenedAt"]
    data["breaker_reset_by"] = row["breakerResetBy"]
    return AutomationDefinition(**data)


def _exec_row(row: Any) -> dict[str, Any]:
    return {
        "id": row["id"],
        "automationId": row["automationId"],
        "automationVersion": row["automationVersion"],
        "runId": row["runId"],
        "taskId": row["taskId"],
        "scopeId": row["scopeId"] if "scopeId" in row.keys() else None,
        "triggerType": row["triggerType"],
        "triggerRef": row["triggerRef"],
        "state": row["state"],
        "attempt": row["attempt"],
        "maxAttempts": row["maxAttempts"],
        "scheduledFor": row["scheduledFor"],
        "startedAt": row["startedAt"],
        "finishedAt": row["finishedAt"],
        "outcomeReason": row["outcomeReason"],
        "errorClass": row["errorClass"],
        "cost": row["cost"],
        "tokens": row["tokens"],
        "workerId": row["workerId"],
        "leaseExpiresAt": row["leaseExpiresAt"],
        "snapshot": json.loads(row["snapshotJson"]) if row["snapshotJson"] else None,
        "dryRun": bool(row["dryRun"]),
        "output": row["output"] if "output" in row.keys() else None,
        "decision": (
            json.loads(row["decision"]) if "decision" in row.keys() and row["decision"] else None
        ),
    }


class AutomationStore:
    """Automation definitions, versions, executions, locks, conditions, deliveries."""

    def __init__(self, store: Any) -> None:
        self._store = store
        self._ensure_schema()

    @property
    def _db(self):
        return self._store._db

    # ── Schema ───────────────────────────────────────────────────────────────
    def _ensure_schema(self) -> None:
        db = self._db
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS Automation (
              id                  TEXT PRIMARY KEY,
              name                TEXT NOT NULL,
              state               TEXT NOT NULL DEFAULT 'draft',
              triggerType         TEXT NOT NULL,
              eventType           TEXT,
              conditionExpression TEXT,
              version             INTEGER NOT NULL DEFAULT 1,
              scopeId             TEXT,
              nextRunAt           TEXT,
              lastRunAt           TEXT,
              createdAt           TEXT NOT NULL,
              updatedAt           TEXT NOT NULL,
              consecutiveFailures INTEGER NOT NULL DEFAULT 0,
              breaker             TEXT NOT NULL DEFAULT 'closed',
              breakerOpenedAt     TEXT,
              breakerResetBy      TEXT,
              definitionJson      TEXT NOT NULL
            )
            """
        )
        db.execute(
            "CREATE INDEX IF NOT EXISTS idx_automation_due ON Automation(state, nextRunAt)"
        )
        db.execute(
            "CREATE INDEX IF NOT EXISTS idx_automation_kind ON Automation(state, triggerType)"
        )
        db.execute(
            "CREATE INDEX IF NOT EXISTS idx_automation_scope ON Automation(scopeId, createdAt)"
        )
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS AutomationVersion (
              id             TEXT NOT NULL,
              version        INTEGER NOT NULL,
              definitionJson TEXT NOT NULL,
              createdAt      TEXT NOT NULL,
              createdBy      TEXT,
              PRIMARY KEY (id, version)
            )
            """
        )
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS AutomationAudit (
              id           TEXT PRIMARY KEY,
              automationId TEXT NOT NULL,
              action       TEXT NOT NULL,
              actor        TEXT,
              detailJson   TEXT,
              createdAt    TEXT NOT NULL
            )
            """
        )
        db.execute(
            "CREATE INDEX IF NOT EXISTS idx_automation_audit ON AutomationAudit(automationId, createdAt)"
        )
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS AutomationExecution (
              id                TEXT PRIMARY KEY,
              automationId      TEXT NOT NULL,
              automationVersion INTEGER NOT NULL,
              runId             TEXT,
              taskId            TEXT,
              scopeId           TEXT,
              triggerType       TEXT NOT NULL,
              triggerRef        TEXT,
              state             TEXT NOT NULL,
              attempt           INTEGER NOT NULL DEFAULT 1,
              maxAttempts       INTEGER NOT NULL DEFAULT 1,
              scheduledFor      TEXT,
              startedAt         TEXT NOT NULL,
              finishedAt        TEXT,
              outcomeReason     TEXT,
              errorClass        TEXT,
              cost              REAL,
              tokens            INTEGER,
              workerId          TEXT,
              leaseExpiresAt    TEXT,
              snapshotJson      TEXT,
              dryRun            INTEGER NOT NULL DEFAULT 0,
              output            TEXT
            )
            """
        )
        db.execute(
            "CREATE INDEX IF NOT EXISTS idx_autoexec_automation "
            "ON AutomationExecution(automationId, startedAt)"
        )
        db.execute(
            "CREATE INDEX IF NOT EXISTS idx_autoexec_state ON AutomationExecution(state)"
        )
        # Additive migration: an older AutomationExecution table predates the
        # stored run `output` (the answer the unattended run produced). CREATE
        # TABLE IF NOT EXISTS would not add it, so ALTER explicitly.
        self._ensure_column("AutomationExecution", "output", "TEXT")
        # Additive: the structured trading decision for a `run_strategy` run. A
        # no-trade run still writes one, so "why didn't it trade?" is answerable.
        self._ensure_column("AutomationExecution", "decision", "TEXT")
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS AutomationExecutionEvent (
              id          TEXT PRIMARY KEY,
              executionId TEXT NOT NULL,
              seq         INTEGER NOT NULL,
              at          TEXT NOT NULL,
              type        TEXT NOT NULL,
              dataJson    TEXT
            )
            """
        )
        db.execute(
            "CREATE INDEX IF NOT EXISTS idx_autoexec_event "
            "ON AutomationExecutionEvent(executionId, seq)"
        )
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS AutomationLock (
              name         TEXT PRIMARY KEY,
              owner        TEXT NOT NULL,
              acquiredAt   TEXT NOT NULL,
              expiresAt    TEXT NOT NULL,
              renewedAt    TEXT
            )
            """
        )
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS AutomationDelivery (
              id          TEXT PRIMARY KEY,
              executionId TEXT NOT NULL,
              adapter     TEXT NOT NULL,
              destination TEXT,
              ok          INTEGER NOT NULL DEFAULT 0,
              reasonCode  TEXT,
              reason      TEXT,
              at          TEXT NOT NULL
            )
            """
        )
        db.execute(
            "CREATE INDEX IF NOT EXISTS idx_autodelivery_exec "
            "ON AutomationDelivery(executionId)"
        )
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS AutomationEventSubscription (
              eventType    TEXT NOT NULL,
              automationId TEXT NOT NULL,
              PRIMARY KEY (eventType, automationId)
            )
            """
        )
        db.execute(
            "CREATE INDEX IF NOT EXISTS idx_autosub_event ON AutomationEventSubscription(eventType)"
        )
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS AutomationConditionState (
              automationId    TEXT PRIMARY KEY,
              lastResult      TEXT,
              previousResult  TEXT,
              valuesJson      TEXT,
              historyJson     TEXT,
              updatedAt       TEXT
            )
            """
        )
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS AutomationEventDedup (
              automationId TEXT NOT NULL,
              eventId      TEXT NOT NULL,
              at           TEXT NOT NULL,
              PRIMARY KEY (automationId, eventId)
            )
            """
        )
        db.execute(
            "CREATE INDEX IF NOT EXISTS idx_autodedup_at ON AutomationEventDedup(at)"
        )
        # One-time maintenance markers (guarded migrations that must run exactly
        # once, so a later deliberate operator choice is never silently reverted).
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS AutomationMaintenance (
              key       TEXT PRIMARY KEY,
              appliedAt TEXT NOT NULL
            )
            """
        )
        db.commit()
        self._migrate_misfire_default()

    def _migrate_misfire_default(self) -> None:
        """One-time: upgrade the old `skip` misfire default to `run_once`.

        `skip` was the shipped default and silently DROPPED every scheduled run
        that came due while Eclipse was closed, so a daily bot only ran if the
        app happened to be open at that minute. `run_once` runs the missed fire
        when the app returns. Guarded so a later, deliberate `skip` is preserved.
        """
        key = "misfire_default_run_once"
        if self._db.execute(
            "SELECT 1 FROM AutomationMaintenance WHERE key=?", (key,)
        ).fetchone():
            return
        rows = self._db.execute("SELECT id, definitionJson FROM Automation").fetchall()
        for row in rows:
            try:
                data = json.loads(row["definitionJson"])
            except (TypeError, ValueError):
                continue
            execution = data.get("execution")
            if isinstance(execution, dict) and execution.get("misfire") == "skip":
                execution["misfire"] = "run_once"
                self._db.execute(
                    "UPDATE Automation SET definitionJson=?, updatedAt=? WHERE id=?",
                    (json.dumps(data), now_iso(), row["id"]),
                )
        self._db.execute(
            "INSERT OR REPLACE INTO AutomationMaintenance(key, appliedAt) VALUES (?, ?)",
            (key, now_iso()),
        )
        self._db.commit()

    # ── Definitions ──────────────────────────────────────────────────────────
    def _ensure_column(self, table: str, name: str, ddl: str) -> None:
        """Idempotently add a column to an existing table (additive migration)."""
        existing = {
            row["name"] for row in self._db.execute(f"PRAGMA table_info({table})").fetchall()
        }
        if name not in existing:
            self._db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")

    def save_automation(
        self, definition: AutomationDefinition, *, created_by: Optional[str] = None,
        snapshot: bool = True,
    ) -> AutomationDefinition:
        """Upsert a definition. A version bump writes an immutable version row."""
        existing = self.get_automation(definition.id)
        if existing is not None and snapshot and existing.version != definition.version:
            self._write_version(definition, created_by=created_by)
        elif existing is None and snapshot:
            self._write_version(definition, created_by=created_by)
        trigger = definition.trigger
        event_type = trigger.event_type if trigger.type == "event" else None
        condition_expr = definition.condition.expression if definition.condition else (
            trigger.condition.expression if trigger.condition else None
        )
        self._db.execute(
            """INSERT OR REPLACE INTO Automation
               (id, name, state, triggerType, eventType, conditionExpression, version,
                scopeId, nextRunAt, lastRunAt, createdAt, updatedAt, consecutiveFailures,
                breaker, breakerOpenedAt, breakerResetBy, definitionJson)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                definition.id, definition.name, definition.state, trigger.type, event_type,
                condition_expr, definition.version, definition.scope_id, definition.next_run_at,
                definition.last_run_at, definition.created_at, definition.updated_at,
                definition.consecutive_failures, definition.breaker,
                definition.breaker_opened_at, definition.breaker_reset_by,
                definition.model_dump_json(by_alias=False),
            ),
        )
        self._sync_subscription(definition)
        self._db.commit()
        return definition

    def _sync_subscription(self, definition: AutomationDefinition) -> None:
        self._db.execute(
            "DELETE FROM AutomationEventSubscription WHERE automationId=?", (definition.id,)
        )
        if definition.trigger.type == "event" and definition.trigger.event_type:
            self._db.execute(
                "INSERT OR REPLACE INTO AutomationEventSubscription(eventType, automationId) "
                "VALUES (?, ?)",
                (definition.trigger.event_type, definition.id),
            )

    def _write_version(self, definition: AutomationDefinition, *, created_by: Optional[str]) -> None:
        self._db.execute(
            "INSERT OR REPLACE INTO AutomationVersion(id, version, definitionJson, createdAt, createdBy) "
            "VALUES (?, ?, ?, ?, ?)",
            (definition.id, definition.version, definition.model_dump_json(by_alias=False),
             now_iso(), created_by),
        )

    def get_automation(self, automation_id: str) -> Optional[AutomationDefinition]:
        row = self._db.execute(
            "SELECT * FROM Automation WHERE id=?", (automation_id,)
        ).fetchone()
        return _def_row(row) if row else None

    def list_automations(
        self, limit: int = 200, *, scope_id: Optional[str] = None, state: Optional[str] = None,
    ) -> list[AutomationDefinition]:
        limit = max(1, min(int(limit), 2000))
        clauses: list[str] = []
        params: list[Any] = []
        if scope_id is not None:
            clauses.append("scopeId=?")
            params.append(scope_id)
        if state is not None:
            clauses.append("state=?")
            params.append(state)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        rows = self._db.execute(
            f"SELECT * FROM Automation {where} ORDER BY createdAt DESC LIMIT ?", tuple(params)
        ).fetchall()
        return [_def_row(r) for r in rows]

    def delete_automation(self, automation_id: str) -> bool:
        """Delete the definition + subscriptions. Execution history is RETAINED."""
        self._db.execute("DELETE FROM AutomationEventSubscription WHERE automationId=?", (automation_id,))
        cur = self._db.execute("DELETE FROM Automation WHERE id=?", (automation_id,))
        self._db.commit()
        return cur.rowcount > 0

    def set_state(self, automation_id: str, state: str, *, reset_by: Optional[str] = None) -> Optional[AutomationDefinition]:
        definition = self.get_automation(automation_id)
        if definition is None:
            return None
        updated = definition.model_copy(update={"state": state, "updated_at": now_iso()})
        return self.save_automation(updated, snapshot=False)

    def set_next_run(self, automation_id: str, next_run_at: Optional[str]) -> None:
        self._db.execute(
            "UPDATE Automation SET nextRunAt=?, updatedAt=? WHERE id=?",
            (next_run_at, now_iso(), automation_id),
        )
        self._db.commit()

    def record_outcome(
        self, automation_id: str, *, status: str, ran_at: str, consecutive_failures: int,
        breaker: str, breaker_opened_at: Optional[str],
    ) -> None:
        self._db.execute(
            """UPDATE Automation
               SET lastRunAt=?, updatedAt=?, consecutiveFailures=?, breaker=?, breakerOpenedAt=?
               WHERE id=?""",
            (ran_at, now_iso(), consecutive_failures, breaker, breaker_opened_at, automation_id),
        )
        self._db.commit()

    def touch_last_run(self, automation_id: str, *, ran_at: str) -> None:
        self._db.execute(
            "UPDATE Automation SET lastRunAt=?, updatedAt=? WHERE id=?",
            (ran_at, now_iso(), automation_id),
        )
        self._db.commit()

    def reset_breaker(self, automation_id: str, *, reset_by: str) -> Optional[AutomationDefinition]:
        self._db.execute(
            """UPDATE Automation
               SET consecutiveFailures=0, breaker='closed', breakerOpenedAt=NULL,
                   breakerResetBy=?, updatedAt=?
               WHERE id=?""",
            (reset_by, now_iso(), automation_id),
        )
        self._db.commit()
        return self.get_automation(automation_id)

    def due_automations(self, now_iso_value: str, *, limit: int = 200) -> list[AutomationDefinition]:
        """Active timed automations whose nextRunAt is due (indexed lookup)."""
        rows = self._db.execute(
            """SELECT * FROM Automation
               WHERE state='active' AND nextRunAt IS NOT NULL AND nextRunAt <= ?
                 AND triggerType IN ('cron','interval','one_time')
               ORDER BY nextRunAt ASC LIMIT ?""",
            (now_iso_value, max(1, min(limit, 1000))),
        ).fetchall()
        return [_def_row(r) for r in rows]

    def condition_automations(self, *, limit: int = 200) -> list[AutomationDefinition]:
        """Active condition-trigger automations (indexed by state+triggerType)."""
        rows = self._db.execute(
            "SELECT * FROM Automation WHERE state='active' AND triggerType='condition' LIMIT ?",
            (max(1, min(limit, 1000)),),
        ).fetchall()
        return [_def_row(r) for r in rows]

    def event_subscribers(self, event_type: str) -> list[AutomationDefinition]:
        rows = self._db.execute(
            """SELECT a.* FROM Automation a
               JOIN AutomationEventSubscription s ON s.automationId = a.id
               WHERE s.eventType=? AND a.state='active'""",
            (event_type,),
        ).fetchall()
        return [_def_row(r) for r in rows]

    def subscribed_event_types(self) -> list[str]:
        rows = self._db.execute(
            "SELECT DISTINCT eventType FROM AutomationEventSubscription"
        ).fetchall()
        return [r["eventType"] for r in rows]

    # ── Versions ─────────────────────────────────────────────────────────────
    def record_audit(
        self,
        automation_id: str,
        action: str,
        *,
        actor: Optional[str] = None,
        detail: Optional[dict[str, Any]] = None,
    ) -> None:
        """Append one operator/agent action to the bot audit trail.

        Best-effort by contract: an audit write failure must never fail the
        domain operation. The caller decides whether the audit is required.
        """
        try:
            self._db.execute(
                "INSERT INTO AutomationAudit (id, automationId, action, actor, detailJson, createdAt) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    f"aud_{uuid.uuid4().hex[:20]}",
                    automation_id,
                    str(action)[:80],
                    str(actor)[:200] if actor else None,
                    json.dumps(detail or {}, sort_keys=True)[:20_000],
                    now_iso(),
                ),
            )
            self._db.commit()
        except Exception:  # noqa: BLE001 - audit is advisory, never a blocker
            pass

    def list_audit(
        self, automation_id: Optional[str] = None, limit: int = 200,
    ) -> list[dict[str, Any]]:
        if automation_id:
            rows = self._db.execute(
                "SELECT * FROM AutomationAudit WHERE automationId = ? "
                "ORDER BY createdAt DESC LIMIT ?",
                (automation_id, max(1, min(1000, int(limit)))),
            ).fetchall()
        else:
            rows = self._db.execute(
                "SELECT * FROM AutomationAudit ORDER BY createdAt DESC LIMIT ?",
                (max(1, min(1000, int(limit))),),
            ).fetchall()
        out: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            raw = item.pop("detailJson", None)
            try:
                item["detail"] = json.loads(raw) if raw else {}
            except (TypeError, ValueError):
                item["detail"] = {}
            out.append(item)
        return out

    def list_versions(self, automation_id: str) -> list[dict[str, Any]]:
        rows = self._db.execute(
            "SELECT * FROM AutomationVersion WHERE id=? ORDER BY version DESC", (automation_id,)
        ).fetchall()
        return [
            {"version": r["version"], "createdAt": r["createdAt"], "createdBy": r["createdBy"],
             "definition": json.loads(r["definitionJson"])}
            for r in rows
        ]

    def get_version(self, automation_id: str, version: int) -> Optional[dict[str, Any]]:
        row = self._db.execute(
            "SELECT * FROM AutomationVersion WHERE id=? AND version=?", (automation_id, version)
        ).fetchone()
        if row is None:
            return None
        return {"version": row["version"], "createdAt": row["createdAt"],
                "createdBy": row["createdBy"], "definition": json.loads(row["definitionJson"])}

    # ── Executions ───────────────────────────────────────────────────────────
    def start_execution(
        self, automation: AutomationDefinition, *, trigger_type: str, trigger_ref: Optional[str],
        scheduled_for: Optional[str], snapshot: dict[str, Any], run_id: Optional[str] = None,
        task_id: Optional[str] = None, state: str = "queued", dry_run: bool = False,
        worker_id: Optional[str] = None, lease_expires_at: Optional[str] = None,
    ) -> dict[str, Any]:
        row = {
            "id": new_execution_id(),
            "automationId": automation.id,
            "automationVersion": automation.version,
            "runId": run_id,
            "taskId": task_id,
            "scopeId": automation.scope_id,
            "triggerType": trigger_type,
            "triggerRef": trigger_ref,
            "state": state,
            "attempt": 1,
            "maxAttempts": max(1, automation.retry.max_attempts),
            "scheduledFor": scheduled_for,
            "startedAt": now_iso(),
            "finishedAt": None,
            "outcomeReason": None,
            "errorClass": None,
            "cost": None,
            "tokens": None,
            "workerId": worker_id,
            "leaseExpiresAt": lease_expires_at,
            "snapshotJson": json.dumps(snapshot),
            "dryRun": 1 if dry_run else 0,
            "output": None,
        }
        self._db.execute(
            """INSERT INTO AutomationExecution
               (id, automationId, automationVersion, runId, taskId, scopeId, triggerType,
                triggerRef, state, attempt, maxAttempts, scheduledFor, startedAt, finishedAt,
                outcomeReason, errorClass, cost, tokens, workerId, leaseExpiresAt, snapshotJson, dryRun, output)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            tuple(row.values()),
        )
        self._db.commit()
        return row

    def update_execution(self, execution_id: str, **fields: Any) -> None:
        allowed = {
            "state", "attempt", "runId", "taskId", "finishedAt", "outcomeReason",
            "errorClass", "cost", "tokens", "workerId", "leaseExpiresAt", "scheduledFor",
        }
        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            return
        assignments = ", ".join(f"{k}=?" for k in updates)
        self._db.execute(
            f"UPDATE AutomationExecution SET {assignments} WHERE id=?",
            (*updates.values(), execution_id),
        )
        self._db.commit()

    def finish_execution(
        self, execution_id: str, *, state: str, outcome_reason: Optional[str] = None,
        error_class: Optional[str] = None, run_id: Optional[str] = None,
        task_id: Optional[str] = None, cost: Optional[float] = None, tokens: Optional[int] = None,
        output: Optional[str] = None, decision: Optional[Any] = None,
    ) -> None:
        payload = json.dumps(decision, sort_keys=True, default=str) if decision else None
        self._db.execute(
            """UPDATE AutomationExecution
               SET state=?, outcomeReason=?, errorClass=?, finishedAt=?,
                   runId=COALESCE(?, runId), taskId=COALESCE(?, taskId),
                   cost=COALESCE(?, cost), tokens=COALESCE(?, tokens),
                   output=COALESCE(?, output), decision=COALESCE(?, decision)
               WHERE id=?""",
            (state, outcome_reason, error_class, now_iso(), run_id, task_id, cost, tokens,
             output, payload, execution_id),
        )
        self._db.commit()

    def get_execution(self, execution_id: str) -> Optional[dict[str, Any]]:
        row = self._db.execute(
            "SELECT * FROM AutomationExecution WHERE id=?", (execution_id,)
        ).fetchone()
        return _exec_row(row) if row else None

    def list_executions(self, automation_id: str, limit: int = 100) -> list[dict[str, Any]]:
        rows = self._db.execute(
            "SELECT * FROM AutomationExecution WHERE automationId=? ORDER BY startedAt DESC LIMIT ?",
            (automation_id, max(1, min(int(limit), 1000))),
        ).fetchall()
        return [_exec_row(r) for r in rows]

    def execution_exists_for_slot(self, automation_id: str, scheduled_for: Optional[str]) -> bool:
        """Whether this automation already has ANY execution for a schedule slot.

        Recovery idempotency: a misfire is keyed by the slot it missed, so a
        process that restarts several times does not record the same missed slot
        (or re-skip it) over and over.
        """
        if not scheduled_for:
            return False
        row = self._db.execute(
            "SELECT 1 FROM AutomationExecution WHERE automationId=? AND scheduledFor=? LIMIT 1",
            (automation_id, scheduled_for),
        ).fetchone()
        return row is not None

    def list_recent_executions(self, limit: int = 100) -> list[dict[str, Any]]:
        rows = self._db.execute(
            "SELECT * FROM AutomationExecution ORDER BY startedAt DESC LIMIT ?",
            (max(1, min(int(limit), 1000)),),
        ).fetchall()
        return [_exec_row(r) for r in rows]

    def active_executions(self) -> list[dict[str, Any]]:
        placeholders = ",".join("?" for _ in TERMINAL_STATES)
        rows = self._db.execute(
            f"SELECT * FROM AutomationExecution WHERE state NOT IN ({placeholders})",
            TERMINAL_STATES,
        ).fetchall()
        return [_exec_row(r) for r in rows]

    def running_count(self, automation_id: str) -> int:
        """Executions occupying a concurrency slot: in-flight OR queued.

        Used by `_launch` to decide a conflict, so a pending (queued) run still
        counts against the cap and the automation does not over-subscribe.
        """
        row = self._db.execute(
            "SELECT COUNT(*) AS n FROM AutomationExecution "
            "WHERE automationId=? AND state NOT IN ('completed','failed','cancelled','timed_out','skipped','blocked')",
            (automation_id,),
        ).fetchone()
        return int(row["n"]) if row else 0

    def in_flight_count(self, automation_id: str) -> int:
        """Executions ACTUALLY running (not queued).

        `_drain_queue` must use this: a queued execution is what it is trying to
        start, so counting it as running (`running_count`) meant the queue never
        drained — the automation looked permanently at its concurrency cap.
        """
        row = self._db.execute(
            "SELECT COUNT(*) AS n FROM AutomationExecution "
            "WHERE automationId=? AND state IN ('triggered','running','waiting','retrying')",
            (automation_id,),
        ).fetchone()
        return int(row["n"]) if row else 0

    def last_execution(self, automation_id: str) -> Optional[dict[str, Any]]:
        row = self._db.execute(
            "SELECT * FROM AutomationExecution WHERE automationId=? ORDER BY startedAt DESC LIMIT 1",
            (automation_id,),
        ).fetchone()
        return _exec_row(row) if row else None

    def last_executions(self) -> dict[str, dict[str, Any]]:
        """The latest execution per automation, in ONE query.

        The sidebar needs only the last run of every bot (unread dot + subtitle).
        Asking `last_execution` once per bot is an N+1 fan-out that grows with the
        list; a window function collapses it to a single indexed scan.
        """
        rows = self._db.execute(
            """
            SELECT * FROM (
              SELECT *, ROW_NUMBER() OVER (
                PARTITION BY automationId ORDER BY startedAt DESC
              ) AS rn
              FROM AutomationExecution
            ) WHERE rn = 1
            """
        ).fetchall()
        return {row["automationId"]: _exec_row(row) for row in rows}

    # ── Execution timeline events ────────────────────────────────────────────
    def append_execution_event(self, execution_id: str, event_type: str, data: Optional[dict[str, Any]] = None) -> None:
        row = self._db.execute(
            "SELECT COALESCE(MAX(seq), 0) AS s FROM AutomationExecutionEvent WHERE executionId=?",
            (execution_id,),
        ).fetchone()
        seq = int(row["s"]) + 1
        self._db.execute(
            "INSERT INTO AutomationExecutionEvent(id, executionId, seq, at, type, dataJson) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (f"evt_{execution_id}_{seq}", execution_id, seq, now_iso(), event_type,
             json.dumps(data) if data is not None else None),
        )
        self._db.commit()

    def list_execution_events(self, execution_id: str) -> list[dict[str, Any]]:
        rows = self._db.execute(
            "SELECT * FROM AutomationExecutionEvent WHERE executionId=? ORDER BY seq ASC",
            (execution_id,),
        ).fetchall()
        return [
            {"seq": r["seq"], "at": r["at"], "type": r["type"],
             "data": json.loads(r["dataJson"]) if r["dataJson"] else None}
            for r in rows
        ]

    # ── Locks / leases (§23) ─────────────────────────────────────────────────
    def acquire_lock(self, name: str, owner: str, *, ttl_s: float) -> bool:
        """Acquire a lease if free or expired. Returns True on success."""
        import datetime as _dt

        now = _dt.datetime.now(_dt.timezone.utc)
        expires = (now + _dt.timedelta(seconds=max(0.001, ttl_s))).isoformat()
        row = self._db.execute("SELECT owner, expiresAt FROM AutomationLock WHERE name=?", (name,)).fetchone()
        if row is not None:
            try:
                current_expiry = _dt.datetime.fromisoformat(row["expiresAt"])
            except ValueError:
                current_expiry = now
            if current_expiry > now:
                return False
            # Expired lease — reclaim (crash recovery).
            self._db.execute(
                "UPDATE AutomationLock SET owner=?, acquiredAt=?, expiresAt=?, renewedAt=NULL WHERE name=?",
                (owner, now.isoformat(), expires, name),
            )
            self._db.commit()
            return True
        self._db.execute(
            "INSERT INTO AutomationLock(name, owner, acquiredAt, expiresAt) VALUES (?, ?, ?, ?)",
            (name, owner, now.isoformat(), expires),
        )
        self._db.commit()
        return True

    def renew_lock(self, name: str, owner: str, *, ttl_s: float) -> bool:
        import datetime as _dt

        now = _dt.datetime.now(_dt.timezone.utc)
        expires = (now + _dt.timedelta(seconds=max(0.001, ttl_s))).isoformat()
        cur = self._db.execute(
            "UPDATE AutomationLock SET expiresAt=?, renewedAt=? WHERE name=? AND owner=?",
            (expires, now.isoformat(), name, owner),
        )
        self._db.commit()
        return cur.rowcount > 0

    def release_lock(self, name: str, owner: str) -> None:
        self._db.execute("DELETE FROM AutomationLock WHERE name=? AND owner=?", (name, owner))
        self._db.commit()

    def reclaim_expired_locks(self) -> int:
        import datetime as _dt

        now = _dt.datetime.now(_dt.timezone.utc).isoformat()
        cur = self._db.execute("DELETE FROM AutomationLock WHERE expiresAt <= ?", (now,))
        self._db.commit()
        return cur.rowcount

    # ── Deliveries ───────────────────────────────────────────────────────────
    def record_delivery(
        self, execution_id: str, *, adapter: str, destination: Optional[str], ok: bool,
        reason_code: Optional[str] = None, reason: Optional[str] = None,
    ) -> None:
        self._db.execute(
            """INSERT INTO AutomationDelivery(id, executionId, adapter, destination, ok, reasonCode, reason, at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (f"del_{uuid.uuid4().hex[:20]}", execution_id, adapter, destination, 1 if ok else 0,
             reason_code, reason, now_iso()),
        )
        self._db.commit()

    def list_deliveries(self, execution_id: str) -> list[dict[str, Any]]:
        rows = self._db.execute(
            "SELECT * FROM AutomationDelivery WHERE executionId=? ORDER BY at ASC", (execution_id,)
        ).fetchall()
        return [
            {"adapter": r["adapter"], "destination": r["destination"], "ok": bool(r["ok"]),
             "reasonCode": r["reasonCode"], "reason": r["reason"], "at": r["at"]}
            for r in rows
        ]

    # ── Condition state (§9) ─────────────────────────────────────────────────
    def get_condition_state(self, automation_id: str) -> Optional[dict[str, Any]]:
        row = self._db.execute(
            "SELECT * FROM AutomationConditionState WHERE automationId=?", (automation_id,)
        ).fetchone()
        if row is None:
            return None
        return {
            "lastResult": row["lastResult"],
            "previousResult": row["previousResult"],
            "values": json.loads(row["valuesJson"]) if row["valuesJson"] else {},
            "history": json.loads(row["historyJson"]) if row["historyJson"] else {},
            "updatedAt": row["updatedAt"],
        }

    def set_condition_state(
        self, automation_id: str, *, last_result: Optional[str], previous_result: Optional[str],
        values: dict[str, Any], history: dict[str, Any],
    ) -> None:
        self._db.execute(
            """INSERT OR REPLACE INTO AutomationConditionState
               (automationId, lastResult, previousResult, valuesJson, historyJson, updatedAt)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (automation_id, last_result, previous_result, json.dumps(values), json.dumps(history), now_iso()),
        )
        self._db.commit()

    # ── Event dedup (§10) ────────────────────────────────────────────────────
    def already_seen_event(self, automation_id: str, event_id: str) -> bool:
        row = self._db.execute(
            "SELECT 1 FROM AutomationEventDedup WHERE automationId=? AND eventId=? LIMIT 1",
            (automation_id, event_id),
        ).fetchone()
        return row is not None

    def mark_event_seen(self, automation_id: str, event_id: str) -> None:
        self._db.execute(
            "INSERT OR IGNORE INTO AutomationEventDedup(automationId, eventId, at) VALUES (?, ?, ?)",
            (automation_id, event_id, now_iso()),
        )
        self._db.commit()

    def prune_event_dedup(self, *, older_than_iso: str) -> int:
        cur = self._db.execute("DELETE FROM AutomationEventDedup WHERE at < ?", (older_than_iso,))
        self._db.commit()
        return cur.rowcount

    # ── Metrics (§43) ────────────────────────────────────────────────────────
    def metrics(self) -> dict[str, Any]:
        def scalar(sql: str, params: tuple = ()) -> int:
            row = self._db.execute(sql, params).fetchone()
            return int(row["n"]) if row else 0

        return {
            "automationCount": scalar("SELECT COUNT(*) AS n FROM Automation"),
            "activeAutomationCount": scalar("SELECT COUNT(*) AS n FROM Automation WHERE state='active'"),
            "pausedAutomationCount": scalar("SELECT COUNT(*) AS n FROM Automation WHERE state='paused'"),
            "disabledAutomationCount": scalar("SELECT COUNT(*) AS n FROM Automation WHERE state='disabled'"),
            "executionCount": scalar("SELECT COUNT(*) AS n FROM AutomationExecution"),
            "successfulExecutionCount": scalar(
                "SELECT COUNT(*) AS n FROM AutomationExecution WHERE state='completed'"
            ),
            "failedExecutionCount": scalar(
                "SELECT COUNT(*) AS n FROM AutomationExecution WHERE state='failed'"
            ),
            "retryCount": scalar(
                "SELECT COUNT(*) AS n FROM AutomationExecution WHERE attempt > 1"
            ),
            "deliveryCount": scalar("SELECT COUNT(*) AS n FROM AutomationDelivery"),
            "openBreakerCount": scalar("SELECT COUNT(*) AS n FROM Automation WHERE breaker='open'"),
        }

    # ── Legacy migration (§49) ───────────────────────────────────────────────
    def migrate_legacy_jobs(self) -> dict[str, int]:
        """Import existing HarnessJob rows into Automation, preserving ids + history.

        Idempotent: an automation whose id already exists is left untouched. Old
        rows are NEVER deleted here — the migration only adds.
        """
        from .legacy import job_to_automation, job_run_to_execution

        try:
            jobs = self._db.execute("SELECT * FROM HarnessJob").fetchall()
        except Exception:  # noqa: BLE001 - no legacy table means nothing to migrate
            return {"migrated": 0, "skipped": 0}
        migrated = 0
        skipped = 0
        for row in jobs:
            try:
                definition = job_to_automation(row)
            except Exception:  # noqa: BLE001 - a malformed legacy row is skipped, not fatal
                skipped += 1
                continue
            if self.get_automation(definition.id) is not None:
                skipped += 1
                continue
            self.save_automation(definition, created_by="migration")
            self._migrate_runs(definition.id, job_run_to_execution)
            migrated += 1
        self._db.commit()
        return {"migrated": migrated, "skipped": skipped}

    def _migrate_runs(self, automation_id: str, converter: Any) -> None:
        try:
            runs = self._db.execute(
                "SELECT * FROM HarnessJobRun WHERE jobId=? ORDER BY startedAt ASC", (automation_id,)
            ).fetchall()
        except Exception:  # noqa: BLE001
            return
        for run in runs:
            execution = converter(run, automation_id)
            if self.get_execution(execution["id"]) is not None:
                continue
            self._db.execute(
                """INSERT OR REPLACE INTO AutomationExecution
                   (id, automationId, automationVersion, runId, taskId, scopeId, triggerType,
                    triggerRef, state, attempt, maxAttempts, scheduledFor, startedAt, finishedAt,
                    outcomeReason, errorClass, cost, tokens, workerId, leaseExpiresAt, snapshotJson, dryRun)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    execution["id"], execution["automationId"], execution["automationVersion"],
                    execution["runId"], execution["taskId"], execution["scopeId"],
                    execution["triggerType"], execution["triggerRef"], execution["state"],
                    execution["attempt"], execution["maxAttempts"], execution["scheduledFor"],
                    execution["startedAt"], execution["finishedAt"], execution["outcomeReason"],
                    execution["errorClass"], execution["cost"], execution["tokens"],
                    execution["workerId"], execution["leaseExpiresAt"],
                    json.dumps(execution["snapshot"]) if execution["snapshot"] else None,
                    execution["dryRun"],
                ),
            )


__all__ = ["AutomationStore", "new_automation_id", "new_execution_id"]
