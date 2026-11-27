"""Portfolio persistence on the ONE `agent-memory.db` connection.

Why here and not `jdds.db`/Prisma: `jdds.db` is the committed market-data seed
vault; portfolio transactions are private user/agent state and must never be
committed or treated as market data. The Python runtime already owns durable
agent state in `agent-memory.db` (runs, browser, jobs, documents, market
snapshots), and every other subsystem persists through the ONE shared
`AgentMemoryStore` connection with idempotent DDL. The Portfolio engine follows
that exact pattern — it adds no second database and no migration step.

The transaction ledger is the source of truth. Tax lots/matches are DERIVED
rows: they are rebuilt from the ledger whenever a mutation touches a symbol and
are never edited directly. Snapshots are historical analytics artifacts.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Iterable, Optional, Sequence

from ..audit_chain import GENESIS, event_hash, verify_chain
from ..persistence import now_iso
from .models import (
    CostBasisMethod,
    TaxLot,
    TaxLotMatch,
    Transaction,
    TransactionType,
)
from .money import D, ZERO, money_str, qty_str

_SCHEMA = """
CREATE TABLE IF NOT EXISTS Portfolio (
  id               TEXT PRIMARY KEY,
  name             TEXT NOT NULL,
  baseCurrency     TEXT NOT NULL DEFAULT 'USD',
  ownerId          TEXT,
  description      TEXT,
  costBasisMethod  TEXT NOT NULL DEFAULT 'FIFO',
  currency         TEXT NOT NULL DEFAULT 'USD',
  status           TEXT NOT NULL DEFAULT 'ACTIVE',
  -- There is ONE user-facing portfolio type: a fund. The transaction ledger is
  -- the fund's accounting substrate; `kind` is retained for compatibility.
  kind             TEXT NOT NULL DEFAULT 'fund',
  metadata         TEXT NOT NULL DEFAULT '{}',
  createdAt        TEXT NOT NULL,
  updatedAt        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_portfolio_owner ON Portfolio(ownerId);

CREATE TABLE IF NOT EXISTS PortfolioAccount (
  id           TEXT PRIMARY KEY,
  portfolioId  TEXT NOT NULL,
  name         TEXT NOT NULL,
  kind         TEXT NOT NULL DEFAULT 'brokerage',
  institution  TEXT,
  currency     TEXT NOT NULL DEFAULT 'USD',
  externalId   TEXT,
  createdAt    TEXT NOT NULL,
  FOREIGN KEY (portfolioId) REFERENCES Portfolio(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_portfolio_account_port ON PortfolioAccount(portfolioId);

CREATE TABLE IF NOT EXISTS PortfolioTransaction (
  id               TEXT PRIMARY KEY,
  portfolioId      TEXT NOT NULL,
  accountId        TEXT,
  instrumentId     TEXT,
  symbol           TEXT,
  assetType        TEXT,
  txnType          TEXT NOT NULL,
  quantity         TEXT NOT NULL DEFAULT '0',
  price            TEXT,
  grossAmount      TEXT,
  fees             TEXT NOT NULL DEFAULT '0',
  taxes            TEXT NOT NULL DEFAULT '0',
  netAmount        TEXT,
  basisAdjustment  TEXT,
  lotSelection     TEXT,
  currency         TEXT NOT NULL DEFAULT 'USD',
  fxRate           TEXT,
  tradeDate        TEXT,
  settlementDate   TEXT,
  timestamp        TEXT,
  externalId       TEXT,
  source           TEXT NOT NULL DEFAULT 'manual',
  notes            TEXT,
  correctionOf     TEXT,
  createdBy        TEXT,
  createdAt        TEXT NOT NULL,
  updatedAt        TEXT NOT NULL,
  FOREIGN KEY (portfolioId) REFERENCES Portfolio(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_portfolio_txn_port ON PortfolioTransaction(portfolioId);
CREATE INDEX IF NOT EXISTS idx_portfolio_txn_symbol ON PortfolioTransaction(portfolioId, symbol);
CREATE INDEX IF NOT EXISTS idx_portfolio_txn_date ON PortfolioTransaction(portfolioId, tradeDate);
CREATE UNIQUE INDEX IF NOT EXISTS uq_portfolio_txn_dedupe
  ON PortfolioTransaction(portfolioId, source, externalId)
  WHERE externalId IS NOT NULL;

CREATE TABLE IF NOT EXISTS PortfolioPolicy (
  portfolioId       TEXT PRIMARY KEY,
  taxRules          TEXT NOT NULL DEFAULT '{}',
  rebalanceMethod   TEXT NOT NULL DEFAULT 'threshold',
  updatedAt         TEXT NOT NULL,
  FOREIGN KEY (portfolioId) REFERENCES Portfolio(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS TaxLot (
  id                  TEXT PRIMARY KEY,
  portfolioId         TEXT NOT NULL,
  symbol              TEXT NOT NULL,
  quantity            TEXT NOT NULL,
  originalQuantity    TEXT NOT NULL,
  costPerUnit         TEXT NOT NULL,
  feeAllocated        TEXT NOT NULL DEFAULT '0',
  currency            TEXT NOT NULL DEFAULT 'USD',
  openedAt            TEXT NOT NULL,
  sourceTransactionId TEXT NOT NULL,
  sequence            INTEGER NOT NULL DEFAULT 0,
  rebuiltAt           TEXT NOT NULL,
  FOREIGN KEY (portfolioId) REFERENCES Portfolio(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_taxlot_port_symbol ON TaxLot(portfolioId, symbol);

CREATE TABLE IF NOT EXISTS TaxLotMatch (
  id                TEXT PRIMARY KEY,
  portfolioId       TEXT NOT NULL,
  symbol            TEXT NOT NULL,
  sellTransactionId TEXT NOT NULL,
  lotId             TEXT NOT NULL,
  quantity          TEXT NOT NULL,
  costBasis         TEXT NOT NULL,
  proceeds          TEXT NOT NULL,
  realizedPnl       TEXT NOT NULL,
  acquiredAt        TEXT NOT NULL,
  disposedAt        TEXT NOT NULL,
  rebuiltAt         TEXT NOT NULL,
  FOREIGN KEY (portfolioId) REFERENCES Portfolio(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_taxmatch_port_symbol ON TaxLotMatch(portfolioId, symbol);
CREATE INDEX IF NOT EXISTS idx_taxmatch_sell ON TaxLotMatch(sellTransactionId);

-- Materialized positions: a per-symbol DERIVED cache rebuilt incrementally on
-- every mutation, so a read does not recompute the whole ledger. It is never the
-- source of truth — a missing/stale row is recomputed from the ledger on read.
CREATE TABLE IF NOT EXISTS PortfolioPosition (
  portfolioId  TEXT NOT NULL,
  symbol       TEXT NOT NULL,
  quantity     TEXT NOT NULL DEFAULT '0',
  costBasis    TEXT NOT NULL DEFAULT '0',
  averageCost  TEXT,
  realizedPnl  TEXT NOT NULL DEFAULT '0',
  currency     TEXT NOT NULL DEFAULT 'USD',
  updatedAt    TEXT NOT NULL,
  PRIMARY KEY (portfolioId, symbol),
  FOREIGN KEY (portfolioId) REFERENCES Portfolio(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_portfolio_position ON PortfolioPosition(portfolioId);

CREATE TABLE IF NOT EXISTS PortfolioPositionMeta (
  portfolioId  TEXT NOT NULL,
  symbol       TEXT NOT NULL,
  name         TEXT,
  assetClass   TEXT,
  sector       TEXT,
  country      TEXT,
  strategy     TEXT,
  currency     TEXT,
  updatedAt    TEXT NOT NULL,
  PRIMARY KEY (portfolioId, symbol),
  FOREIGN KEY (portfolioId) REFERENCES Portfolio(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS PortfolioBenchmark (
  id           TEXT PRIMARY KEY,
  portfolioId  TEXT NOT NULL,
  symbol       TEXT NOT NULL,
  label        TEXT,
  kind         TEXT NOT NULL DEFAULT 'index',
  weight       TEXT NOT NULL DEFAULT '0',
  createdAt    TEXT NOT NULL,
  FOREIGN KEY (portfolioId) REFERENCES Portfolio(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_portfolio_benchmark ON PortfolioBenchmark(portfolioId);

CREATE TABLE IF NOT EXISTS PortfolioSnapshot (
  id              TEXT PRIMARY KEY,
  portfolioId     TEXT NOT NULL,
  asOf            TEXT NOT NULL,
  baseCurrency    TEXT NOT NULL DEFAULT 'USD',
  totalValue      TEXT,
  cash            TEXT,
  investedValue   TEXT,
  realizedPnl     TEXT,
  unrealizedPnl   TEXT,
  returnPct       TEXT,
  dataQuality     TEXT NOT NULL DEFAULT 'OK',
  payload         TEXT,
  createdBy       TEXT,
  createdAt       TEXT NOT NULL,
  FOREIGN KEY (portfolioId) REFERENCES Portfolio(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_portfolio_snapshot ON PortfolioSnapshot(portfolioId, asOf);

CREATE TABLE IF NOT EXISTS PortfolioReconciliation (
  id           TEXT PRIMARY KEY,
  portfolioId  TEXT NOT NULL,
  source       TEXT,
  asOf         TEXT NOT NULL,
  status       TEXT NOT NULL,
  payload      TEXT,
  createdBy    TEXT,
  createdAt    TEXT NOT NULL,
  FOREIGN KEY (portfolioId) REFERENCES Portfolio(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_portfolio_recon ON PortfolioReconciliation(portfolioId, asOf);

CREATE TABLE IF NOT EXISTS RebalancePlan (
  id           TEXT PRIMARY KEY,
  portfolioId  TEXT NOT NULL,
  asOf         TEXT NOT NULL,
  method       TEXT NOT NULL DEFAULT 'threshold',
  status       TEXT NOT NULL DEFAULT 'PROPOSED',
  target       TEXT NOT NULL DEFAULT '{}',
  payload      TEXT,
  createdBy    TEXT,
  createdAt    TEXT NOT NULL,
  FOREIGN KEY (portfolioId) REFERENCES Portfolio(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_rebalance_plan ON RebalancePlan(portfolioId);

CREATE TABLE IF NOT EXISTS RebalanceRecommendation (
  id             TEXT PRIMARY KEY,
  planId         TEXT NOT NULL,
  portfolioId    TEXT NOT NULL,
  symbol         TEXT NOT NULL,
  action         TEXT NOT NULL,
  quantity       TEXT,
  price          TEXT,
  estimatedValue TEXT,
  estFees        TEXT,
  taxImpact      TEXT,
  estRealizedPnl TEXT,
  reason         TEXT,
  createdAt      TEXT NOT NULL,
  FOREIGN KEY (planId) REFERENCES RebalancePlan(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_rebalance_rec_plan ON RebalanceRecommendation(planId);

CREATE TABLE IF NOT EXISTS PortfolioAuditEvent (
  id           TEXT PRIMARY KEY,
  portfolioId  TEXT NOT NULL,
  kind         TEXT NOT NULL,
  actor        TEXT,
  reason       TEXT,
  before       TEXT,
  after        TEXT,
  createdAt    TEXT NOT NULL,
  prevHash     TEXT,
  hash         TEXT,
  FOREIGN KEY (portfolioId) REFERENCES Portfolio(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_portfolio_audit ON PortfolioAuditEvent(portfolioId, createdAt);

-- Immutable construction/optimization run record (§38): the exact inputs,
-- constraints, solver status, independently-validated target weights, input
-- snapshot reference and reproducibility metadata for one construction run.
-- portfolioId is nullable so universe-only research runs are captured too.
CREATE TABLE IF NOT EXISTS PortfolioConstructionRun (
  id           TEXT PRIMARY KEY,
  portfolioId  TEXT,
  method       TEXT NOT NULL,
  status       TEXT NOT NULL,
  symbols      TEXT NOT NULL DEFAULT '[]',
  target       TEXT NOT NULL DEFAULT '{}',
  constraints  TEXT,
  validation   TEXT,
  payload      TEXT,
  dataVersion  TEXT,
  engineVersion TEXT,
  createdBy    TEXT,
  createdAt    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_construction_run ON PortfolioConstructionRun(portfolioId, createdAt);
"""


def _loads(value: Any) -> Any:
    if value is None:
        return None
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return value


def _dumps(value: Any) -> str:
    return json.dumps(value, default=str)


def _portfolio_audit_payload(row: Any) -> dict[str, Any]:
    """The immutable fields sealed by one portfolio audit event's hash.

    `before`/`after` are the STORED text (or NULL), not a re-serialization, so
    verification does not depend on a future writer's JSON formatting.
    """
    return {
        "id": row["id"],
        "portfolioId": row["portfolioId"],
        "kind": row["kind"],
        "actor": row["actor"],
        "reason": row["reason"],
        "before": row["before"],
        "after": row["after"],
        "createdAt": row["createdAt"],
    }


def _construction_row(row: Any) -> dict[str, Any]:
    data = dict(row)
    for key in ("symbols", "target", "constraints", "validation", "payload"):
        data[key] = _loads(data.get(key))
    return data


class PortfolioStore:
    """CRUD over the portfolio tables on the shared agent-memory connection."""

    def __init__(self, store: Any) -> None:
        self._store = store
        self._ensure_schema()

    @property
    def _db(self) -> sqlite3.Connection:
        return self._store._db

    def _ensure_schema(self) -> None:
        self._db.executescript(_SCHEMA)
        self._add_missing_columns()
        # There is one user-facing portfolio type now: a fund. Existing ledger
        # rows are migrated in place (no data loss); the transaction ledger
        # remains the fund's accounting substrate.
        self._db.execute("UPDATE Portfolio SET kind='fund' WHERE kind IS NULL OR kind != 'fund'")
        self._db.execute(
            "UPDATE Portfolio SET metadata=? WHERE metadata IS NULL OR metadata='' OR metadata='{}'",
            (json.dumps({"kind": "fund"}),),
        )
        self._db.commit()

    def _add_missing_columns(self) -> None:
        """Additive, non-destructive column migration for pre-existing DBs.

        `CREATE TABLE IF NOT EXISTS` never alters an existing table, so a column
        added after first release must be ALTER-ed in. Only adds; never drops or
        rewrites data.
        """
        wanted = {
            "PortfolioTransaction": {"basisAdjustment": "TEXT", "lotSelection": "TEXT"},
            "Portfolio": {"kind": "TEXT NOT NULL DEFAULT 'fund'", "metadata": "TEXT NOT NULL DEFAULT '{}'"},
            "PortfolioAuditEvent": {"prevHash": "TEXT", "hash": "TEXT"},
        }
        for table, columns in wanted.items():
            existing = {r["name"] for r in self._db.execute(f"PRAGMA table_info({table})").fetchall()}
            for column, decl in columns.items():
                if column not in existing:
                    self._db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
        self._seal_audit_chain()

    def _seal_audit_chain(self) -> None:
        """Seal audit rows written before the chain existed (metadata only)."""
        pending = self._db.execute(
            "SELECT COUNT(*) AS n FROM PortfolioAuditEvent WHERE hash IS NULL"
        ).fetchone()
        if not pending or not int(pending["n"] or 0):
            return
        portfolio_ids = [row["portfolioId"] for row in self._db.execute(
            "SELECT DISTINCT portfolioId FROM PortfolioAuditEvent WHERE hash IS NULL"
        ).fetchall()]
        for portfolio_id in portfolio_ids:
            previous = GENESIS
            rows = self._db.execute(
                "SELECT * FROM PortfolioAuditEvent WHERE portfolioId=? ORDER BY rowid ASC",
                (portfolio_id,),
            ).fetchall()
            for row in rows:
                payload = _portfolio_audit_payload(row)
                sealed = event_hash(previous, payload)
                self._db.execute(
                    "UPDATE PortfolioAuditEvent SET prevHash=?, hash=? WHERE id=?",
                    (previous, sealed, row["id"]),
                )
                previous = sealed
        self._db.commit()

    # ── Portfolios ───────────────────────────────────────────────────────────
    def create_portfolio(
        self,
        portfolio_id: str,
        name: str,
        *,
        base_currency: str = "USD",
        owner_id: Optional[str] = None,
        description: Optional[str] = None,
        cost_basis_method: str = "FIFO",
        kind: str = "fund",
        metadata: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        now = now_iso()
        metadata_json = json.dumps(metadata or {}, ensure_ascii=False, default=str)
        self._db.execute(
            """INSERT INTO Portfolio
               (id, name, baseCurrency, ownerId, description, costBasisMethod, currency, status,
                kind, metadata, createdAt, updatedAt)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'ACTIVE', ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET
                 name=excluded.name, baseCurrency=excluded.baseCurrency, ownerId=excluded.ownerId,
                 description=excluded.description, costBasisMethod=excluded.costBasisMethod,
                 currency=excluded.currency, kind=excluded.kind, metadata=excluded.metadata,
                 updatedAt=excluded.updatedAt""",
            (
                portfolio_id, name, base_currency, owner_id, description, cost_basis_method,
                base_currency, kind, metadata_json, now, now,
            ),
        )
        self._db.commit()
        return self.get_portfolio(portfolio_id)  # type: ignore[return-value]

    def get_portfolio(self, portfolio_id: str) -> Optional[dict[str, Any]]:
        row = self._db.execute("SELECT * FROM Portfolio WHERE id = ?", (portfolio_id,)).fetchone()
        return dict(row) if row is not None else None

    def list_portfolios(self, owner_id: Optional[str] = None) -> list[dict[str, Any]]:
        if owner_id:
            rows = self._db.execute(
                "SELECT * FROM Portfolio WHERE ownerId = ? OR ownerId IS NULL ORDER BY createdAt",
                (owner_id,),
            ).fetchall()
        else:
            rows = self._db.execute("SELECT * FROM Portfolio ORDER BY createdAt").fetchall()
        return [dict(r) for r in rows]

    def update_portfolio(self, portfolio_id: str, **fields: Any) -> Optional[dict[str, Any]]:
        allowed = {"name", "baseCurrency", "description", "costBasisMethod", "status", "ownerId", "kind", "metadata"}
        sets = []
        values: list[Any] = []
        for key, value in fields.items():
            if key in allowed and value is not None:
                if key == "metadata" and isinstance(value, dict):
                    value = json.dumps(value, ensure_ascii=False, default=str)
                sets.append(f"{key} = ?")
                values.append(value)
        if sets:
            sets.append("updatedAt = ?")
            values.append(now_iso())
            values.append(portfolio_id)
            self._db.execute(f"UPDATE Portfolio SET {', '.join(sets)} WHERE id = ?", values)
            self._db.commit()
        return self.get_portfolio(portfolio_id)

    def delete_portfolio(self, portfolio_id: str) -> bool:
        cur = self._db.execute("DELETE FROM Portfolio WHERE id = ?", (portfolio_id,))
        self._db.commit()
        return cur.rowcount > 0

    # ── Accounts ─────────────────────────────────────────────────────────────
    def create_account(self, account_id: str, portfolio_id: str, name: str, **fields: Any) -> dict[str, Any]:
        self._db.execute(
            """INSERT INTO PortfolioAccount (id, portfolioId, name, kind, institution, currency, externalId, createdAt)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET
                 name=excluded.name, kind=excluded.kind, institution=excluded.institution,
                 currency=excluded.currency, externalId=excluded.externalId""",
            (
                account_id, portfolio_id, name,
                fields.get("kind", "brokerage"), fields.get("institution"),
                fields.get("currency", "USD"), fields.get("external_id"), now_iso(),
            ),
        )
        self._db.commit()
        return self.get_account(account_id)  # type: ignore[return-value]

    def get_account(self, account_id: str) -> Optional[dict[str, Any]]:
        row = self._db.execute("SELECT * FROM PortfolioAccount WHERE id = ?", (account_id,)).fetchone()
        return dict(row) if row is not None else None

    def list_accounts(self, portfolio_id: str) -> list[dict[str, Any]]:
        rows = self._db.execute(
            "SELECT * FROM PortfolioAccount WHERE portfolioId = ? ORDER BY createdAt", (portfolio_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    def delete_account(self, portfolio_id: str, account_id: str) -> bool:
        cur = self._db.execute(
            "DELETE FROM PortfolioAccount WHERE id=? AND portfolioId=?",
            (account_id, portfolio_id),
        )
        self._db.commit()
        return cur.rowcount > 0

    # ── Transactions ─────────────────────────────────────────────────────────
    def append_transaction(self, tx: Transaction) -> tuple[Transaction, bool]:
        """Insert a transaction idempotently. Returns (transaction, was_duplicate).

        A transaction with a non-null `external_id` is deduplicated on
        (portfolioId, source, externalId). A duplicate returns the EXISTING row
        rather than writing a second copy — safe under API/worker/network retry.
        """
        try:
            self._insert_transaction(tx)
            self._db.commit()
            return self.get_transaction(tx.id) or tx, False
        except sqlite3.IntegrityError:
            existing = None
            if tx.external_id:
                row = self._db.execute(
                    "SELECT * FROM PortfolioTransaction WHERE portfolioId=? AND source=? AND externalId=?",
                    (tx.portfolio_id, tx.source, tx.external_id),
                ).fetchone()
                if row is not None:
                    existing = self._row_to_transaction(row)
            if existing is None:
                raise
            return existing, True

    def _insert_transaction(self, tx: Transaction) -> None:
        self._db.execute(
            """INSERT INTO PortfolioTransaction
               (id, portfolioId, accountId, instrumentId, symbol, assetType, txnType,
                quantity, price, grossAmount, fees, taxes, netAmount, basisAdjustment, lotSelection, currency, fxRate,
                tradeDate, settlementDate, timestamp, externalId, source, notes, correctionOf,
                createdBy, createdAt, updatedAt)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                tx.id, tx.portfolio_id, tx.account_id, tx.instrument_id, tx.symbol, tx.asset_type,
                tx.transaction_type.value, qty_str(tx.quantity), money_str(tx.price),
                money_str(tx.gross_amount), money_str(tx.fees), money_str(tx.taxes),
                money_str(tx.net_amount), money_str(tx.basis_adjustment), _dumps(tx.lot_selection),
                tx.currency, money_str(tx.fx_rate),
                tx.trade_date, tx.settlement_date, tx.timestamp, tx.external_id, tx.source,
                tx.notes, tx.correction_of, tx.created_by, tx.created_at, tx.updated_at,
            ),
        )

    def get_transaction(self, transaction_id: str) -> Optional[Transaction]:
        row = self._db.execute(
            "SELECT * FROM PortfolioTransaction WHERE id = ?", (transaction_id,)
        ).fetchone()
        return self._row_to_transaction(row) if row is not None else None

    def list_transactions(
        self, portfolio_id: str, *, symbol: Optional[str] = None, limit: int = 5000
    ) -> list[Transaction]:
        if symbol:
            rows = self._db.execute(
                """SELECT * FROM PortfolioTransaction WHERE portfolioId=? AND symbol=?
                   ORDER BY COALESCE(tradeDate, timestamp, createdAt), createdAt, rowid LIMIT ?""",
                (portfolio_id, symbol, limit),
            ).fetchall()
        else:
            rows = self._db.execute(
                """SELECT * FROM PortfolioTransaction WHERE portfolioId=?
                   ORDER BY COALESCE(tradeDate, timestamp, createdAt), createdAt, rowid LIMIT ?""",
                (portfolio_id, limit),
            ).fetchall()
        return [self._row_to_transaction(r) for r in rows]

    def delete_transaction(self, transaction_id: str) -> bool:
        cur = self._db.execute("DELETE FROM PortfolioTransaction WHERE id = ?", (transaction_id,))
        self._db.commit()
        return cur.rowcount > 0

    @staticmethod
    def _row_to_transaction(row: Any) -> Transaction:
        def opt(key: str):
            value = row[key]
            return None if value in (None, "") else value

        return Transaction(
            id=row["id"],
            portfolio_id=row["portfolioId"],
            account_id=row["accountId"],
            instrument_id=row["instrumentId"],
            symbol=row["symbol"],
            asset_type=row["assetType"],
            transaction_type=TransactionType(row["txnType"]),
            quantity=D(row["quantity"] or "0"),
            price=(D(row["price"]) if opt("price") else None),
            gross_amount=(D(row["grossAmount"]) if opt("grossAmount") else None),
            basis_adjustment=(D(row["basisAdjustment"]) if opt("basisAdjustment") else None),
            lot_selection=(_loads(row["lotSelection"]) if opt("lotSelection") else None),
            fees=D(row["fees"] or "0"),
            taxes=D(row["taxes"] or "0"),
            net_amount=(D(row["netAmount"]) if opt("netAmount") else None),
            currency=row["currency"] or "USD",
            fx_rate=(D(row["fxRate"]) if opt("fxRate") else None),
            trade_date=row["tradeDate"],
            settlement_date=row["settlementDate"],
            timestamp=row["timestamp"],
            external_id=row["externalId"],
            source=row["source"] or "manual",
            notes=row["notes"],
            correction_of=row["correctionOf"],
            created_by=row["createdBy"],
            created_at=row["createdAt"],
            updated_at=row["updatedAt"],
        )

    # ── Derived lots / matches ───────────────────────────────────────────────
    def replace_accounting(
        self, portfolio_id: str, symbol: str, lots: Sequence[TaxLot], matches: Sequence[TaxLotMatch]
    ) -> None:
        now = now_iso()
        with self._db:
            self._db.execute(
                "DELETE FROM TaxLot WHERE portfolioId=? AND symbol=?", (portfolio_id, symbol)
            )
            self._db.execute(
                "DELETE FROM TaxLotMatch WHERE portfolioId=? AND symbol=?", (portfolio_id, symbol)
            )
            for lot in lots:
                self._db.execute(
                    """INSERT INTO TaxLot
                       (id, portfolioId, symbol, quantity, originalQuantity, costPerUnit, feeAllocated,
                        currency, openedAt, sourceTransactionId, sequence, rebuiltAt)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        lot.id, portfolio_id, symbol, qty_str(lot.quantity), qty_str(lot.original_quantity),
                        money_str(lot.cost_per_unit), money_str(lot.fee_allocated), lot.currency,
                        lot.opened_at, lot.source_transaction_id, lot.sequence, now,
                    ),
                )
            for match in matches:
                self._db.execute(
                    """INSERT INTO TaxLotMatch
                       (id, portfolioId, symbol, sellTransactionId, lotId, quantity, costBasis,
                        proceeds, realizedPnl, acquiredAt, disposedAt, rebuiltAt)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        match.id, portfolio_id, symbol, match.sell_transaction_id, match.lot_id,
                        qty_str(match.quantity), money_str(match.cost_basis), money_str(match.proceeds),
                        money_str(match.realized_pnl), match.acquired_at, match.disposed_at, now,
                    ),
                )

    def list_lots(self, portfolio_id: str, symbol: Optional[str] = None) -> list[TaxLot]:
        if symbol:
            rows = self._db.execute(
                "SELECT * FROM TaxLot WHERE portfolioId=? AND symbol=? ORDER BY sequence",
                (portfolio_id, symbol),
            ).fetchall()
        else:
            rows = self._db.execute(
                "SELECT * FROM TaxLot WHERE portfolioId=? ORDER BY symbol, sequence", (portfolio_id,)
            ).fetchall()
        return [self._row_to_lot(r) for r in rows]

    @staticmethod
    def _row_to_lot(row: Any) -> TaxLot:
        return TaxLot(
            id=row["id"],
            portfolio_id=row["portfolioId"],
            symbol=row["symbol"],
            quantity=D(row["quantity"]),
            original_quantity=D(row["originalQuantity"]),
            cost_per_unit=D(row["costPerUnit"]),
            fee_allocated=D(row["feeAllocated"] or "0"),
            currency=row["currency"],
            opened_at=row["openedAt"],
            source_transaction_id=row["sourceTransactionId"],
            sequence=int(row["sequence"]),
        )

    def list_matches(self, portfolio_id: str, symbol: Optional[str] = None) -> list[TaxLotMatch]:
        if symbol:
            rows = self._db.execute(
                "SELECT * FROM TaxLotMatch WHERE portfolioId=? AND symbol=? ORDER BY disposedAt",
                (portfolio_id, symbol),
            ).fetchall()
        else:
            rows = self._db.execute(
                "SELECT * FROM TaxLotMatch WHERE portfolioId=? ORDER BY symbol, disposedAt",
                (portfolio_id,),
            ).fetchall()
        return [self._row_to_match(r) for r in rows]

    @staticmethod
    def _row_to_match(row: Any) -> TaxLotMatch:
        return TaxLotMatch(
            id=row["id"],
            portfolio_id=row["portfolioId"],
            symbol=row["symbol"],
            sell_transaction_id=row["sellTransactionId"],
            lot_id=row["lotId"],
            quantity=D(row["quantity"]),
            cost_basis=D(row["costBasis"]),
            proceeds=D(row["proceeds"]),
            realized_pnl=D(row["realizedPnl"]),
            acquired_at=row["acquiredAt"],
            disposed_at=row["disposedAt"],
        )

    # ── Position metadata ────────────────────────────────────────────────────
    def set_position_meta(self, portfolio_id: str, symbol: str, **fields: Any) -> None:
        self._db.execute(
            """INSERT INTO PortfolioPositionMeta
               (portfolioId, symbol, name, assetClass, sector, country, strategy, currency, updatedAt)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(portfolioId, symbol) DO UPDATE SET
                 name=COALESCE(excluded.name, PortfolioPositionMeta.name),
                 assetClass=COALESCE(excluded.assetClass, PortfolioPositionMeta.assetClass),
                 sector=COALESCE(excluded.sector, PortfolioPositionMeta.sector),
                 country=COALESCE(excluded.country, PortfolioPositionMeta.country),
                 strategy=COALESCE(excluded.strategy, PortfolioPositionMeta.strategy),
                 currency=COALESCE(excluded.currency, PortfolioPositionMeta.currency),
                 updatedAt=excluded.updatedAt""",
            (
                portfolio_id, symbol, fields.get("name"), fields.get("asset_class"),
                fields.get("sector"), fields.get("country"), fields.get("strategy"),
                fields.get("currency"), now_iso(),
            ),
        )
        self._db.commit()

    def list_position_meta(self, portfolio_id: str) -> dict[str, dict[str, Any]]:
        rows = self._db.execute(
            "SELECT * FROM PortfolioPositionMeta WHERE portfolioId=?", (portfolio_id,)
        ).fetchall()
        return {r["symbol"]: dict(r) for r in rows}

    # ── Materialized positions (derived cache) ───────────────────────────────
    def upsert_materialized_position(
        self, portfolio_id: str, symbol: str, *, quantity: str, cost_basis: str,
        average_cost: Optional[str], realized_pnl: str, currency: str,
    ) -> None:
        self._db.execute(
            """INSERT INTO PortfolioPosition
               (portfolioId, symbol, quantity, costBasis, averageCost, realizedPnl, currency, updatedAt)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(portfolioId, symbol) DO UPDATE SET
                 quantity=excluded.quantity, costBasis=excluded.costBasis,
                 averageCost=excluded.averageCost, realizedPnl=excluded.realizedPnl,
                 currency=excluded.currency, updatedAt=excluded.updatedAt""",
            (portfolio_id, symbol, quantity, cost_basis, average_cost, realized_pnl, currency, now_iso()),
        )
        self._db.commit()

    def get_materialized_position(self, portfolio_id: str, symbol: str) -> Optional[dict[str, Any]]:
        row = self._db.execute(
            "SELECT * FROM PortfolioPosition WHERE portfolioId=? AND symbol=?",
            (portfolio_id, symbol),
        ).fetchone()
        return dict(row) if row is not None else None

    def list_materialized_positions(self, portfolio_id: str) -> dict[str, dict[str, Any]]:
        rows = self._db.execute(
            "SELECT * FROM PortfolioPosition WHERE portfolioId=?", (portfolio_id,)
        ).fetchall()
        return {r["symbol"]: dict(r) for r in rows}

    def delete_materialized_position(self, portfolio_id: str, symbol: Optional[str] = None) -> None:
        if symbol:
            self._db.execute(
                "DELETE FROM PortfolioPosition WHERE portfolioId=? AND symbol=?", (portfolio_id, symbol)
            )
        else:
            self._db.execute("DELETE FROM PortfolioPosition WHERE portfolioId=?", (portfolio_id,))
        self._db.commit()

    # ── Policy ───────────────────────────────────────────────────────────────
    def set_policy(self, portfolio_id: str, *, tax_rules: Optional[dict] = None, rebalance_method: Optional[str] = None) -> None:
        existing = self.get_policy(portfolio_id) or {}
        self._db.execute(
            """INSERT INTO PortfolioPolicy (portfolioId, taxRules, rebalanceMethod, updatedAt)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(portfolioId) DO UPDATE SET
                 taxRules=excluded.taxRules, rebalanceMethod=excluded.rebalanceMethod, updatedAt=excluded.updatedAt""",
            (
                portfolio_id,
                _dumps(tax_rules if tax_rules is not None else existing.get("taxRules", {})),
                rebalance_method or existing.get("rebalanceMethod", "threshold"),
                now_iso(),
            ),
        )
        self._db.commit()

    def get_policy(self, portfolio_id: str) -> Optional[dict[str, Any]]:
        row = self._db.execute(
            "SELECT * FROM PortfolioPolicy WHERE portfolioId=?", (portfolio_id,)
        ).fetchone()
        if row is None:
            return None
        data = dict(row)
        data["taxRules"] = _loads(data.get("taxRules")) or {}
        return data

    # ── Benchmarks ───────────────────────────────────────────────────────────
    def add_benchmark(self, benchmark_id: str, portfolio_id: str, symbol: str, **fields: Any) -> None:
        self._db.execute(
            """INSERT INTO PortfolioBenchmark (id, portfolioId, symbol, label, kind, weight, createdAt)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                benchmark_id, portfolio_id, symbol, fields.get("label"), fields.get("kind", "index"),
                money_str(D(fields.get("weight", "0"))) or "0", now_iso(),
            ),
        )
        self._db.commit()

    def list_benchmarks(self, portfolio_id: str) -> list[dict[str, Any]]:
        rows = self._db.execute(
            "SELECT * FROM PortfolioBenchmark WHERE portfolioId=? ORDER BY createdAt", (portfolio_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    # ── Snapshots ────────────────────────────────────────────────────────────
    def save_snapshot(
        self, snapshot_id: str, portfolio_id: str, *, as_of: str, base_currency: str,
        total_value: Optional[str], cash: Optional[str], invested_value: Optional[str],
        realized_pnl: Optional[str], unrealized_pnl: Optional[str], return_pct: Optional[str],
        data_quality: str, payload: Optional[dict], created_by: Optional[str],
    ) -> None:
        self._db.execute(
            """INSERT INTO PortfolioSnapshot
               (id, portfolioId, asOf, baseCurrency, totalValue, cash, investedValue, realizedPnl,
                unrealizedPnl, returnPct, dataQuality, payload, createdBy, createdAt)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                snapshot_id, portfolio_id, as_of, base_currency, total_value, cash, invested_value,
                realized_pnl, unrealized_pnl, return_pct, data_quality,
                None if payload is None else _dumps(payload), created_by, now_iso(),
            ),
        )
        self._db.commit()

    def list_snapshots(self, portfolio_id: str, limit: int = 365) -> list[dict[str, Any]]:
        rows = self._db.execute(
            "SELECT * FROM PortfolioSnapshot WHERE portfolioId=? ORDER BY asOf DESC LIMIT ?",
            (portfolio_id, max(1, int(limit))),
        ).fetchall()
        out = []
        for r in rows:
            data = dict(r)
            data["payload"] = _loads(data.get("payload"))
            out.append(data)
        return out

    # ── Rebalance plans ──────────────────────────────────────────────────────
    def save_rebalance_plan(
        self, plan_id: str, portfolio_id: str, *, as_of: str, method: str, status: str,
        target: dict, payload: dict, recommendations: Iterable[dict], created_by: Optional[str],
    ) -> None:
        now = now_iso()
        with self._db:
            self._db.execute(
                """INSERT INTO RebalancePlan (id, portfolioId, asOf, method, status, target, payload, createdBy, createdAt)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (plan_id, portfolio_id, as_of, method, status, _dumps(target), _dumps(payload), created_by, now),
            )
            for rec in recommendations:
                self._db.execute(
                    """INSERT INTO RebalanceRecommendation
                       (id, planId, portfolioId, symbol, action, quantity, price, estimatedValue,
                        estFees, taxImpact, estRealizedPnl, reason, createdAt)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        rec["id"], plan_id, portfolio_id, rec["symbol"], rec["action"],
                        rec.get("quantity"), rec.get("price"), rec.get("estimatedValue"),
                        rec.get("estFees"), rec.get("taxImpact"), rec.get("estRealizedPnl"),
                        rec.get("reason"), now,
                    ),
                )

    def list_rebalance_plans(self, portfolio_id: str, limit: int = 50) -> list[dict[str, Any]]:
        rows = self._db.execute(
            "SELECT * FROM RebalancePlan WHERE portfolioId=? ORDER BY createdAt DESC LIMIT ?",
            (portfolio_id, max(1, int(limit))),
        ).fetchall()
        out = []
        for r in rows:
            data = dict(r)
            data["target"] = _loads(data.get("target"))
            data["payload"] = _loads(data.get("payload"))
            recs = self._db.execute(
                "SELECT * FROM RebalanceRecommendation WHERE planId=? ORDER BY symbol", (r["id"],)
            ).fetchall()
            data["recommendations"] = [dict(x) for x in recs]
            out.append(data)
        return out

    # ── Reconciliation ───────────────────────────────────────────────────────
    def save_reconciliation(self, recon_id: str, portfolio_id: str, *, source: Optional[str],
                            as_of: str, status: str, payload: dict, created_by: Optional[str]) -> None:
        self._db.execute(
            """INSERT INTO PortfolioReconciliation
               (id, portfolioId, source, asOf, status, payload, createdBy, createdAt)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (recon_id, portfolio_id, source, as_of, status, _dumps(payload), created_by, now_iso()),
        )
        self._db.commit()

    def list_reconciliations(self, portfolio_id: str, limit: int = 50) -> list[dict[str, Any]]:
        rows = self._db.execute(
            "SELECT * FROM PortfolioReconciliation WHERE portfolioId=? ORDER BY asOf DESC LIMIT ?",
            (portfolio_id, max(1, int(limit))),
        ).fetchall()
        out = []
        for r in rows:
            data = dict(r)
            data["payload"] = _loads(data.get("payload"))
            out.append(data)
        return out

    # ── Audit ────────────────────────────────────────────────────────────────
    def append_audit(
        self, event_id: str, portfolio_id: str, kind: str, *, actor: Optional[str] = None,
        reason: Optional[str] = None, before: Any = None, after: Any = None,
    ) -> None:
        created_at = now_iso()
        before_text = None if before is None else _dumps(before)
        after_text = None if after is None else _dumps(after)
        last = self._db.execute(
            "SELECT hash FROM PortfolioAuditEvent WHERE portfolioId=? AND hash IS NOT NULL "
            "ORDER BY rowid DESC LIMIT 1",
            (portfolio_id,),
        ).fetchone()
        previous = last["hash"] if last and last["hash"] else GENESIS
        payload = {
            "id": event_id, "portfolioId": portfolio_id, "kind": kind,
            "actor": actor, "reason": reason,
            "before": before_text, "after": after_text, "createdAt": created_at,
        }
        sealed = event_hash(previous, payload)
        self._db.execute(
            """INSERT INTO PortfolioAuditEvent
               (id, portfolioId, kind, actor, reason, before, after, createdAt, prevHash, hash)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                event_id, portfolio_id, kind, actor, reason,
                before_text, after_text, created_at, previous, sealed,
            ),
        )
        self._db.commit()

    def list_audit(self, portfolio_id: str, limit: int = 200) -> list[dict[str, Any]]:
        rows = self._db.execute(
            "SELECT * FROM PortfolioAuditEvent WHERE portfolioId=? ORDER BY rowid DESC LIMIT ?",
            (portfolio_id, max(1, int(limit))),
        ).fetchall()
        out = []
        for r in rows:
            data = dict(r)
            data["before"] = _loads(data.get("before"))
            data["after"] = _loads(data.get("after"))
            out.append(data)
        return out

    def verify_audit(self, portfolio_id: str) -> dict[str, Any]:
        """Recompute the portfolio audit chain in append order."""
        rows = self._db.execute(
            "SELECT * FROM PortfolioAuditEvent WHERE portfolioId=? ORDER BY rowid ASC",
            (portfolio_id,),
        ).fetchall()
        result = verify_chain(rows, payload_of=_portfolio_audit_payload)
        result["portfolioId"] = portfolio_id
        return result

    # ── Construction / optimization runs ─────────────────────────────────────
    def save_construction_run(
        self, run_id: str, portfolio_id: Optional[str], *, method: str, status: str,
        symbols: Sequence[str], target: dict[str, Any], constraints: Any, validation: Any,
        payload: dict[str, Any], data_version: Optional[str] = None,
        engine_version: Optional[str] = None, created_by: Optional[str] = None,
    ) -> None:
        self._db.execute(
            """INSERT INTO PortfolioConstructionRun
               (id, portfolioId, method, status, symbols, target, constraints, validation,
                payload, dataVersion, engineVersion, createdBy, createdAt)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                run_id, portfolio_id, method, status, _dumps(list(symbols)), _dumps(target),
                None if constraints is None else _dumps(constraints),
                None if validation is None else _dumps(validation),
                _dumps(payload), data_version, engine_version, created_by, now_iso(),
            ),
        )
        self._db.commit()

    def get_construction_run(self, run_id: str) -> Optional[dict[str, Any]]:
        row = self._db.execute(
            "SELECT * FROM PortfolioConstructionRun WHERE id=?", (run_id,)
        ).fetchone()
        return _construction_row(row) if row is not None else None

    def list_construction_runs(self, portfolio_id: str, limit: int = 50) -> list[dict[str, Any]]:
        rows = self._db.execute(
            "SELECT * FROM PortfolioConstructionRun WHERE portfolioId=? ORDER BY createdAt DESC LIMIT ?",
            (portfolio_id, max(1, int(limit))),
        ).fetchall()
        return [_construction_row(r) for r in rows]

    # ── Health ───────────────────────────────────────────────────────────────
    def count_transactions(self, portfolio_id: str) -> int:
        row = self._db.execute(
            "SELECT COUNT(*) AS n FROM PortfolioTransaction WHERE portfolioId=?", (portfolio_id,)
        ).fetchone()
        return int(row["n"]) if row else 0


__all__ = ["PortfolioStore", "CostBasisMethod", "ZERO"]
