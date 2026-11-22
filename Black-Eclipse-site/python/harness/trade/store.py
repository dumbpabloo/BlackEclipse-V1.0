"""Durable broker connections on the ONE `agent-memory.db` connection.

Two tables, idempotent DDL, no migration step:

  * `BrokerConnection` - the non-secret settings, the last known status and the
    last account snapshot. A credential VALUE is never stored (see
    `model.validate_config`).
  * `BrokerConnectionEvent` - an append-only audit of tests/reads/failures, so a
    broker that stopped answering is explainable after the fact.

Nothing here is authoritative for money. The broker is. This table only remembers
where to look and what the last honest answer was.
"""

from __future__ import annotations

import json
import uuid
from typing import Any, Optional

from ..persistence import now_iso
from .model import (
    BrokerConfigError,
    BrokerConnection,
    ConnectionMode,
    normalise_mode,
    validate_config,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS BrokerConnection (
  id           TEXT PRIMARY KEY,
  kind         TEXT NOT NULL,
  label        TEXT NOT NULL,
  mode         TEXT NOT NULL DEFAULT 'unknown',
  config       TEXT NOT NULL DEFAULT '{}',
  status       TEXT NOT NULL DEFAULT 'unconfigured',
  statusReason TEXT,
  account      TEXT,
  armedUntil   TEXT,
  armedAt      TEXT,
  armedBy      TEXT,
  lastSeenAt   TEXT,
  createdAt    TEXT NOT NULL,
  updatedAt    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_broker_connection_kind ON BrokerConnection(kind);

CREATE TABLE IF NOT EXISTS BrokerConnectionEvent (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  connectionId TEXT NOT NULL,
  kind         TEXT NOT NULL,
  outcome      TEXT NOT NULL,
  detail       TEXT,
  createdAt    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_broker_event_connection
  ON BrokerConnectionEvent(connectionId, id);

CREATE TABLE IF NOT EXISTS BrokerOrder (
  id           TEXT PRIMARY KEY,
  connectionId TEXT NOT NULL,
  intent       TEXT NOT NULL,
  digest       TEXT NOT NULL,
  approvalId   TEXT,
  mandateId    TEXT,
  runId        TEXT,
  nodeId       TEXT,
  status       TEXT NOT NULL,
  ticket       INTEGER,
  retcode      INTEGER,
  retcodeName  TEXT,
  filledVolume REAL,
  avgPrice     REAL,
  error        TEXT,
  createdAt    TEXT NOT NULL,
  updatedAt    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_broker_order_connection
  ON BrokerOrder(connectionId, createdAt);
CREATE INDEX IF NOT EXISTS idx_broker_order_digest ON BrokerOrder(digest);

CREATE TABLE IF NOT EXISTS BrokerFill (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  connectionId   TEXT NOT NULL,
  dealTicket     INTEGER NOT NULL,
  orderTicket    INTEGER,
  positionTicket INTEGER,
  symbol         TEXT NOT NULL,
  side           TEXT NOT NULL,
  entry          TEXT,
  volume         REAL NOT NULL,
  price          REAL NOT NULL,
  commission     REAL NOT NULL DEFAULT 0,
  swap           REAL NOT NULL DEFAULT 0,
  profit         REAL NOT NULL DEFAULT 0,
  magic          INTEGER NOT NULL DEFAULT 0,
  comment        TEXT,
  dealtAt        INTEGER NOT NULL,
  eclipseOwned   INTEGER NOT NULL DEFAULT 0,
  syncedAt       TEXT NOT NULL,
  UNIQUE(connectionId, dealTicket)
);
CREATE INDEX IF NOT EXISTS idx_broker_fill_connection
  ON BrokerFill(connectionId, dealtAt);

CREATE TABLE IF NOT EXISTS BrokerPositionSnapshot (
  connectionId  TEXT NOT NULL,
  ticket        INTEGER NOT NULL,
  symbol        TEXT NOT NULL,
  side          TEXT NOT NULL,
  volume        REAL NOT NULL,
  openPrice     REAL NOT NULL,
  currentPrice  REAL NOT NULL,
  stopLoss      REAL,
  takeProfit    REAL,
  profit        REAL NOT NULL DEFAULT 0,
  swap          REAL NOT NULL DEFAULT 0,
  magic         INTEGER NOT NULL DEFAULT 0,
  comment       TEXT,
  openedAt      INTEGER NOT NULL,
  syncedAt      TEXT NOT NULL,
  PRIMARY KEY(connectionId, ticket)
);

CREATE TABLE IF NOT EXISTS BrokerSyncState (
  connectionId   TEXT PRIMARY KEY,
  lastSyncedAt   TEXT,
  lastDealAt     INTEGER,
  status         TEXT NOT NULL DEFAULT 'never',
  error          TEXT,
  positionsCount INTEGER NOT NULL DEFAULT 0,
  fillsAdded     INTEGER NOT NULL DEFAULT 0,
  updatedAt      TEXT NOT NULL
);
"""

_MAX_LABEL = 80


def _loads(value: Optional[str], fallback: Any) -> Any:
    if not value:
        return fallback
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return fallback


def _int_or_none(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _row(row: Any) -> BrokerConnection:
    return BrokerConnection(
        id=row["id"],
        kind=row["kind"],
        label=row["label"],
        mode=normalise_mode(row["mode"]),
        config=_loads(row["config"], {}),
        status=row["status"] or "unconfigured",
        status_reason=row["statusReason"],
        account=_loads(row["account"], None),
        armed_until=row["armedUntil"] if "armedUntil" in row.keys() else None,
        armed_at=row["armedAt"] if "armedAt" in row.keys() else None,
        armed_by=row["armedBy"] if "armedBy" in row.keys() else None,
        last_seen_at=row["lastSeenAt"],
        created_at=row["createdAt"],
        updated_at=row["updatedAt"],
    )


class BrokerConnectionStore:
    def __init__(self, store: Any) -> None:
        self._store = store
        self._ensure_schema()

    @property
    def _db(self):
        return self._store._db

    def _ensure_schema(self) -> None:
        self._db.executescript(_SCHEMA)
        # Supervisor equity snapshots: the day/week OPEN equity a loss stop is
        # measured from. One row per (scope, period); the first write of a period
        # wins, so a restart cannot move the baseline to hide a loss.
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS BrokerRiskSnapshot (
              scope     TEXT NOT NULL,
              period    TEXT NOT NULL,
              equity    REAL NOT NULL,
              startedAt TEXT NOT NULL,
              PRIMARY KEY (scope, period)
            );
            """
        )
        # Additive columns for databases created before live arming (P2).
        existing = {row["name"] for row in self._db.execute(
            "PRAGMA table_info(BrokerConnection)"
        ).fetchall()}
        for name in ("armedUntil", "armedAt", "armedBy"):
            if name not in existing:
                self._db.execute(f"ALTER TABLE BrokerConnection ADD COLUMN {name} TEXT")
        order_columns = {row["name"] for row in self._db.execute(
            "PRAGMA table_info(BrokerOrder)"
        ).fetchall()}
        if "mandateId" not in order_columns:
            self._db.execute("ALTER TABLE BrokerOrder ADD COLUMN mandateId TEXT")
        # Phase P8: a graph order records which run/node placed it, exactly like
        # the paper fill log, so a ledger row can be traced back to a workflow.
        for name in ("runId", "nodeId"):
            if name not in order_columns:
                self._db.execute(f"ALTER TABLE BrokerOrder ADD COLUMN {name} TEXT")
        # P4: which BOT (and which version of its objective) placed the order. A
        # run id already links to the automation execution, but without this an
        # operator must join two tables to answer "what has this bot actually
        # done?" — and a version bump must be visible on the order it produced.
        for name in ("automationId", "automationVersion"):
            if name not in order_columns:
                self._db.execute(f"ALTER TABLE BrokerOrder ADD COLUMN {name} TEXT")
        self._db.commit()

    # ── reads ───────────────────────────────────────────────────────────────
    def list(self) -> list[BrokerConnection]:
        rows = self._db.execute(
            "SELECT * FROM BrokerConnection ORDER BY createdAt ASC"
        ).fetchall()
        return [_row(r) for r in rows]

    def get(self, connection_id: str) -> Optional[BrokerConnection]:
        row = self._db.execute(
            "SELECT * FROM BrokerConnection WHERE id=?", (str(connection_id or ""),)
        ).fetchone()
        return _row(row) if row else None

    # ── writes ──────────────────────────────────────────────────────────────
    def create(
        self,
        *,
        kind: str,
        label: str,
        mode: Any = ConnectionMode.UNKNOWN,
        config: Optional[dict[str, Any]] = None,
    ) -> BrokerConnection:
        kind = str(kind or "").strip().lower()
        if not kind:
            raise BrokerConfigError("kind is required")
        name = str(label or "").strip()[:_MAX_LABEL]
        if not name:
            raise BrokerConfigError("label is required")
        cleaned = validate_config(config)
        now = now_iso()
        connection_id = f"brk_{uuid.uuid4().hex[:12]}"
        self._db.execute(
            """INSERT INTO BrokerConnection
               (id, kind, label, mode, config, status, createdAt, updatedAt)
               VALUES (?, ?, ?, ?, ?, 'unconfigured', ?, ?)""",
            (
                connection_id,
                kind,
                name,
                normalise_mode(mode).value,
                json.dumps(cleaned),
                now,
                now,
            ),
        )
        self._db.commit()
        return self.get(connection_id)  # type: ignore[return-value]

    def update(self, connection_id: str, patch: dict[str, Any]) -> Optional[BrokerConnection]:
        current = self.get(connection_id)
        if current is None:
            return None
        sets: list[str] = []
        params: list[Any] = []
        if "label" in patch:
            name = str(patch.get("label") or "").strip()[:_MAX_LABEL]
            if not name:
                raise BrokerConfigError("label cannot be empty")
            sets.append("label=?")
            params.append(name)
        if "mode" in patch:
            sets.append("mode=?")
            params.append(normalise_mode(patch.get("mode")).value)
        if "config" in patch:
            cleaned = validate_config(patch.get("config") or {})
            sets.append("config=?")
            params.append(json.dumps(cleaned))
        if not sets:
            return current
        sets.append("updatedAt=?")
        params.append(now_iso())
        params.append(connection_id)
        self._db.execute(
            f"UPDATE BrokerConnection SET {', '.join(sets)} WHERE id=?", params
        )
        self._db.commit()
        return self.get(connection_id)

    def delete(self, connection_id: str) -> bool:
        cursor = self._db.execute(
            "DELETE FROM BrokerConnection WHERE id=?", (str(connection_id or ""),)
        )
        self._db.commit()
        return cursor.rowcount > 0

    def set_status(
        self,
        connection_id: str,
        *,
        status: str,
        reason: Optional[str] = None,
        account: Optional[dict[str, Any]] = None,
        mark_seen: bool = False,
    ) -> Optional[BrokerConnection]:
        now = now_iso()
        sets = ["status=?", "statusReason=?", "updatedAt=?"]
        params: list[Any] = [status, reason, now]
        if account is not None:
            sets.append("account=?")
            params.append(json.dumps(account))
        if mark_seen:
            sets.append("lastSeenAt=?")
            params.append(now)
        params.append(connection_id)
        self._db.execute(
            f"UPDATE BrokerConnection SET {', '.join(sets)} WHERE id=?", params
        )
        self._db.commit()
        return self.get(connection_id)

    def set_arm(
        self, connection_id: str, *, until: str, by: str = "operator",
    ) -> Optional[BrokerConnection]:
        now = now_iso()
        self._db.execute(
            """UPDATE BrokerConnection
               SET armedUntil=?, armedAt=?, armedBy=?, updatedAt=? WHERE id=?""",
            (until, now, by, now, connection_id),
        )
        self._db.commit()
        return self.get(connection_id)

    def clear_arm(self, connection_id: str) -> Optional[BrokerConnection]:
        now = now_iso()
        self._db.execute(
            """UPDATE BrokerConnection
               SET armedUntil=NULL, armedAt=NULL, armedBy=NULL, updatedAt=?
               WHERE id=?""",
            (now, connection_id),
        )
        self._db.commit()
        return self.get(connection_id)

    # ── audit ────────────────────────────────────────────────────────────────
    def record_event(
        self,
        connection_id: str,
        kind: str,
        outcome: str,
        detail: Optional[dict[str, Any]] = None,
    ) -> None:
        self._db.execute(
            """INSERT INTO BrokerConnectionEvent
               (connectionId, kind, outcome, detail, createdAt)
               VALUES (?, ?, ?, ?, ?)""",
            (
                connection_id,
                str(kind or "read"),
                str(outcome or "unknown"),
                json.dumps(detail or {}),
                now_iso(),
            ),
        )
        self._db.commit()

    def list_events(self, connection_id: str, limit: int = 20) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit or 20), 200))
        rows = self._db.execute(
            """SELECT * FROM BrokerConnectionEvent
               WHERE connectionId=? ORDER BY id DESC LIMIT ?""",
            (str(connection_id or ""), limit),
        ).fetchall()
        return [
            {
                "id": r["id"],
                "connectionId": r["connectionId"],
                "kind": r["kind"],
                "outcome": r["outcome"],
                "detail": _loads(r["detail"], {}),
                "createdAt": r["createdAt"],
            }
            for r in rows
        ]

    # ── submitted-order ledger ───────────────────────────────────────────────
    @staticmethod
    def _order_row(row: Any) -> dict[str, Any]:
        return {
            "id": row["id"],
            "connectionId": row["connectionId"],
            "intent": _loads(row["intent"], {}),
            "digest": row["digest"],
            "approvalId": row["approvalId"],
            "mandateId": row["mandateId"] if "mandateId" in row.keys() else None,
            "runId": row["runId"] if "runId" in row.keys() else None,
            "nodeId": row["nodeId"] if "nodeId" in row.keys() else None,
            "automationId": row["automationId"] if "automationId" in row.keys() else None,
            "automationVersion": _int_or_none(
                row["automationVersion"] if "automationVersion" in row.keys() else None
            ),
            "status": row["status"],
            "ticket": row["ticket"],
            "retcode": row["retcode"],
            "retcodeName": row["retcodeName"],
            "filledVolume": row["filledVolume"],
            "avgPrice": row["avgPrice"],
            "error": row["error"],
            "createdAt": row["createdAt"],
            "updatedAt": row["updatedAt"],
        }

    def create_order(
        self,
        *,
        connection_id: str,
        intent: dict[str, Any],
        digest: str,
        approval_id: str = "",
        mandate_id: str = "",
        run_id: str = "",
        node_id: str = "",
        automation_id: str = "",
        automation_version: int | None = None,
        status: str = "submitting",
    ) -> dict[str, Any]:
        now = now_iso()
        order_id = f"ord_{uuid.uuid4().hex[:12]}"
        self._db.execute(
            """INSERT INTO BrokerOrder
               (id, connectionId, intent, digest, approvalId, mandateId, runId, nodeId,
                automationId, automationVersion, status, createdAt, updatedAt)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                order_id,
                str(connection_id),
                json.dumps(intent, sort_keys=True),
                str(digest),
                str(approval_id or "") or None,
                str(mandate_id or "") or None,
                str(run_id or "") or None,
                str(node_id or "") or None,
                str(automation_id or "") or None,
                str(automation_version) if automation_version is not None else None,
                str(status),
                now,
                now,
            ),
        )
        self._db.commit()
        return self.get_order(order_id)  # type: ignore[return-value]

    def update_order(self, order_id: str, **fields: Any) -> Optional[dict[str, Any]]:
        allowed = {
            "status", "ticket", "retcode", "retcodeName",
            "filledVolume", "avgPrice", "error",
        }
        sets: list[str] = []
        params: list[Any] = []
        for key, value in fields.items():
            if key not in allowed:
                continue
            sets.append(f"{key}=?")
            params.append(value)
        if not sets:
            return self.get_order(order_id)
        sets.append("updatedAt=?")
        params.append(now_iso())
        params.append(order_id)
        self._db.execute(f"UPDATE BrokerOrder SET {', '.join(sets)} WHERE id=?", params)
        self._db.commit()
        return self.get_order(order_id)

    def get_order(self, order_id: str) -> Optional[dict[str, Any]]:
        row = self._db.execute(
            "SELECT * FROM BrokerOrder WHERE id=?", (str(order_id or ""),)
        ).fetchone()
        return self._order_row(row) if row else None

    def list_orders(
        self, connection_id: Optional[str] = None, limit: int = 50,
        automation_id: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit or 50), 200))
        clauses: list[str] = []
        params: list[Any] = []
        if connection_id:
            clauses.append("connectionId=?")
            params.append(connection_id)
        if automation_id:
            clauses.append("automationId=?")
            params.append(str(automation_id))
        where = f"WHERE {' AND '.join(clauses)} " if clauses else ""
        rows = self._db.execute(
            f"SELECT * FROM BrokerOrder {where}ORDER BY createdAt DESC LIMIT ?",
            (*params, limit),
        ).fetchall()
        return [self._order_row(row) for row in rows]

    # ── reconciliation: fills, positions, sync state ─────────────────────────
    def save_fills(self, connection_id: str, fills: list[dict[str, Any]]) -> int:
        """Insert new deals; the (connection, dealTicket) unique key dedupes."""
        now = now_iso()
        added = 0
        for fill in fills:
            cursor = self._db.execute(
                """INSERT OR IGNORE INTO BrokerFill
                   (connectionId, dealTicket, orderTicket, positionTicket, symbol,
                    side, entry, volume, price, commission, swap, profit, magic,
                    comment, dealtAt, eclipseOwned, syncedAt)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    str(connection_id),
                    int(fill.get("dealTicket") or 0),
                    int(fill.get("orderTicket") or 0) or None,
                    int(fill.get("positionTicket") or 0) or None,
                    str(fill.get("symbol") or ""),
                    str(fill.get("side") or ""),
                    str(fill.get("entry") or "") or None,
                    float(fill.get("volume") or 0),
                    float(fill.get("price") or 0),
                    float(fill.get("commission") or 0),
                    float(fill.get("swap") or 0),
                    float(fill.get("profit") or 0),
                    int(fill.get("magic") or 0),
                    str(fill.get("comment") or "") or None,
                    int(fill.get("dealtAt") or 0),
                    1 if fill.get("eclipseOwned") else 0,
                    now,
                ),
            )
            added += max(0, cursor.rowcount)
        self._db.commit()
        return added

    # -- supervisor snapshots -------------------------------------------------
    def get_risk_snapshot(self, scope: str, period: str) -> Optional[dict[str, Any]]:
        row = self._db.execute(
            "SELECT scope, period, equity, startedAt FROM BrokerRiskSnapshot "
            "WHERE scope=? AND period=?",
            (str(scope), str(period)),
        ).fetchone()
        return dict(row) if row else None

    def set_risk_snapshot(
        self, scope: str, period: str, equity: float, started_at: str,
    ) -> None:
        """Create the period baseline once; a later call never moves it."""
        self._db.execute(
            "INSERT OR IGNORE INTO BrokerRiskSnapshot "
            "(scope, period, equity, startedAt) VALUES (?, ?, ?, ?)",
            (str(scope), str(period), float(equity), str(started_at)),
        )
        self._db.commit()

    def list_fills(
        self,
        connection_id: str,
        limit: int = 100,
        before_dealt_at: Optional[int] = None,
        before_id: Optional[int] = None,
    ) -> list[dict[str, Any]]:
        """Fills newest-first, optionally older than a (dealtAt, id) cursor.

        The cursor is `(dealtAt, id)` so two deals in the same millisecond page
        correctly. 5,000 is the per-page ceiling; a caller pages with the last
        row's `dealtAt`/`id` instead of asking for an unbounded payload.
        """
        limit = max(1, min(int(limit or 100), 5000))
        clauses = ["connectionId=?"]
        params: list[Any] = [str(connection_id)]
        if before_dealt_at is not None and before_id is not None:
            clauses.append("(dealtAt < ? OR (dealtAt = ? AND id < ?))")
            params.extend([int(before_dealt_at), int(before_dealt_at), int(before_id)])
        rows = self._db.execute(
            f"""SELECT * FROM BrokerFill WHERE {' AND '.join(clauses)}
                ORDER BY dealtAt DESC, id DESC LIMIT ?""",
            (*params, limit),
        ).fetchall()
        return [
            {
                "id": row["id"],
                "dealTicket": row["dealTicket"],
                "orderTicket": row["orderTicket"],
                "positionTicket": row["positionTicket"],
                "symbol": row["symbol"],
                "side": row["side"],
                "entry": row["entry"],
                "volume": row["volume"],
                "price": row["price"],
                "commission": row["commission"],
                "swap": row["swap"],
                "profit": row["profit"],
                "magic": row["magic"],
                "comment": row["comment"],
                "dealtAt": row["dealtAt"],
                "eclipseOwned": bool(row["eclipseOwned"]),
                "syncedAt": row["syncedAt"],
            }
            for row in rows
        ]

    def last_deal_at(self, connection_id: str) -> int:
        row = self._db.execute(
            "SELECT MAX(dealtAt) AS last FROM BrokerFill WHERE connectionId=?",
            (str(connection_id),),
        ).fetchone()
        return int(row["last"] or 0) if row else 0

    def replace_positions(self, connection_id: str, positions: list[dict[str, Any]]) -> int:
        now = now_iso()
        self._db.execute("BEGIN IMMEDIATE")
        try:
            self._db.execute(
                "DELETE FROM BrokerPositionSnapshot WHERE connectionId=?",
                (str(connection_id),),
            )
            for position in positions:
                self._db.execute(
                    """INSERT OR REPLACE INTO BrokerPositionSnapshot
                       (connectionId, ticket, symbol, side, volume, openPrice,
                        currentPrice, stopLoss, takeProfit, profit, swap, magic,
                        comment, openedAt, syncedAt)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        str(connection_id),
                        int(position.get("ticket") or 0),
                        str(position.get("symbol") or ""),
                        str(position.get("side") or ""),
                        float(position.get("volume") or 0),
                        float(position.get("openPrice") or 0),
                        float(position.get("currentPrice") or 0),
                        position.get("stopLoss"),
                        position.get("takeProfit"),
                        float(position.get("profit") or 0),
                        float(position.get("swap") or 0),
                        int(position.get("magic") or 0),
                        str(position.get("comment") or "") or None,
                        int(position.get("openedAt") or 0),
                        now,
                    ),
                )
            self._db.commit()
            return len(positions)
        except Exception:
            self._db.rollback()
            raise

    def list_positions(self, connection_id: str) -> list[dict[str, Any]]:
        rows = self._db.execute(
            "SELECT * FROM BrokerPositionSnapshot WHERE connectionId=? ORDER BY symbol",
            (str(connection_id),),
        ).fetchall()
        return [
            {
                "ticket": row["ticket"],
                "symbol": row["symbol"],
                "side": row["side"],
                "volume": row["volume"],
                "openPrice": row["openPrice"],
                "currentPrice": row["currentPrice"],
                "stopLoss": row["stopLoss"],
                "takeProfit": row["takeProfit"],
                "profit": row["profit"],
                "swap": row["swap"],
                "magic": row["magic"],
                "comment": row["comment"],
                "openedAt": row["openedAt"],
                "syncedAt": row["syncedAt"],
            }
            for row in rows
        ]

    def set_sync_state(
        self,
        connection_id: str,
        *,
        status: str,
        error: Optional[str] = None,
        positions_count: int = 0,
        fills_added: int = 0,
        last_deal_at: Optional[int] = None,
    ) -> None:
        now = now_iso()
        self._db.execute(
            """INSERT INTO BrokerSyncState
               (connectionId, lastSyncedAt, lastDealAt, status, error,
                positionsCount, fillsAdded, updatedAt)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(connectionId) DO UPDATE SET
                 lastSyncedAt=excluded.lastSyncedAt,
                 lastDealAt=COALESCE(excluded.lastDealAt, BrokerSyncState.lastDealAt),
                 status=excluded.status,
                 error=excluded.error,
                 positionsCount=excluded.positionsCount,
                 fillsAdded=excluded.fillsAdded,
                 updatedAt=excluded.updatedAt""",
            (
                str(connection_id),
                now,
                last_deal_at,
                str(status),
                error,
                int(positions_count),
                int(fills_added),
                now,
            ),
        )
        self._db.commit()

    def get_sync_state(self, connection_id: str) -> Optional[dict[str, Any]]:
        row = self._db.execute(
            "SELECT * FROM BrokerSyncState WHERE connectionId=?",
            (str(connection_id),),
        ).fetchone()
        if row is None:
            return None
        return {
            "connectionId": row["connectionId"],
            "lastSyncedAt": row["lastSyncedAt"],
            "lastDealAt": row["lastDealAt"],
            "status": row["status"],
            "error": row["error"],
            "positionsCount": row["positionsCount"],
            "fillsAdded": row["fillsAdded"],
            "updatedAt": row["updatedAt"],
        }


__all__ = ["BrokerConnectionStore"]
