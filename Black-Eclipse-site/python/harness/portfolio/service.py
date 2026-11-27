"""PortfolioService — the ONE orchestrator for the Portfolio Accounting Engine.

Pipeline: transactions → tax lots → positions → valuation → P&L → exposure →
allocation → performance → risk → rebalancing. The transaction ledger is the
source of truth; every derived number is recomputed from it (with lots/matches
persisted for audit and tax queries).

Market prices/FX are read through the existing Market Data Gateway, or through
caller-supplied `marks` (the HTTP front door and tests). A missing price yields
an explicit UNAVAILABLE state — never a fabricated value.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import Any, Optional, Sequence

from . import analytics
from .accounting import AccountingResult, compute_accounting, lot_selection_error
from .analytics import _pct
from .errors import (
    ACCOUNT_EXISTS,
    ACCOUNT_IN_USE,
    ACCOUNT_NOT_FOUND,
    BASIS_UNAVAILABLE,
    INVALID_ACCOUNT,
    INVALID_AMOUNT,
    INVALID_PRICE,
    INVALID_TARGET,
    INVALID_TRANSACTION,
    NEGATIVE_QUANTITY,
    PORTFOLIO_FORBIDDEN,
    PORTFOLIO_NOT_FOUND,
    UNSUPPORTED_CORPORATE_ACTION,
    UNSUPPORTED_TRANSACTION,
    ZERO_QUANTITY,
    PortfolioError,
)
from .market_adapter import MarketAdapter
from .tax_report import build_realized_gains_report
from .models import (
    ACQUISITIONS,
    BASIS_ADJUSTING,
    DISPOSALS,
    INSTRUMENT_EVENTS,
    REALIZED_ONLY,
    CostBasisMethod,
    Mark,
    Transaction,
    TransactionType,
    new_id,
    now_iso as _now_iso,
)
from .money import D, D_or_none, ZERO, money_str, q_qty, qty_str

CALCULATION_VERSION = "portfolio-engine/1.0.0"


def _portfolio_metadata(raw: Any) -> dict[str, Any]:
    import json

    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {}
        except (TypeError, ValueError):
            return {}
    return {}


class PortfolioService:
    def __init__(self, store: Any, *, gateway: Any = None, locks: Any = None) -> None:
        self.store = store
        self.gateway = gateway
        # Attached by the SDK boot layer. A directly constructed service has no
        # lock registry and therefore no locks — never consult another runtime's
        # registry (it may belong to a different, closed database).
        self._locks = locks

    def _assert_unlocked(self, portfolio_id: str, action: str = "write") -> None:
        if self._locks is not None:
            self._locks.assert_allowed("portfolio", portfolio_id, action)

    # ── Portfolios ───────────────────────────────────────────────────────────
    def create_portfolio(self, name: str, **fields: Any) -> dict[str, Any]:
        portfolio_id = fields.get("id") or new_id("pf")
        # There is ONE user-facing portfolio type: a fund. The transaction ledger
        # is the fund's accounting substrate, not a separate "ledger portfolio".
        metadata = fields.get("metadata")
        metadata = dict(metadata) if isinstance(metadata, dict) else {}
        metadata["kind"] = "fund"
        record = self.store.create_portfolio(
            portfolio_id,
            name,
            base_currency=fields.get("base_currency") or fields.get("baseCurrency") or "USD",
            owner_id=fields.get("owner_id") or fields.get("actor_id"),
            description=fields.get("description"),
            cost_basis_method=fields.get("cost_basis_method") or fields.get("costBasisMethod") or "FIFO",
            kind="fund",
            metadata=metadata,
        )
        self.store.append_audit(
            new_id("audit"), portfolio_id, "PORTFOLIO_CREATED",
            actor=fields.get("actor"), after={"name": name},
        )
        return record

    def update_fund(self, portfolio_id: str, fields: dict[str, Any],
                    *, actor_id: Optional[str] = None) -> dict[str, Any]:
        """Update a fund's user-facing metadata (name, goal, icon, accent)."""
        record = self._require_portfolio(portfolio_id, actor_id)
        self._assert_unlocked(portfolio_id, "write")
        metadata = _portfolio_metadata(record.get("metadata"))
        incoming = fields.get("metadata")
        if isinstance(incoming, dict):
            metadata.update(incoming)
        metadata["kind"] = "fund"
        updated = self.store.update_portfolio(
            portfolio_id,
            name=fields.get("name"),
            description=fields.get("description"),
            baseCurrency=fields.get("base_currency") or fields.get("baseCurrency"),
            costBasisMethod=fields.get("cost_basis_method") or fields.get("costBasisMethod"),
            metadata=metadata,
        )
        self.store.append_audit(
            new_id("audit"), portfolio_id, "FUND_UPDATED",
            actor=actor_id, after={"name": fields.get("name"), "metadata": metadata},
        )
        return updated or record

    # ── Accounts (custody / cash separation) ─────────────────────────────────
    def _require_account(self, portfolio_id: str, account_id: str) -> dict[str, Any]:
        account = self.store.get_account(str(account_id or ""))
        if account is None or account.get("portfolioId") != portfolio_id:
            raise PortfolioError(
                ACCOUNT_NOT_FOUND,
                f"account {account_id} not found in portfolio {portfolio_id}",
                {"accountId": account_id, "portfolioId": portfolio_id},
            )
        return account

    def create_account(
        self,
        portfolio_id: str,
        name: str,
        *,
        kind: str = "brokerage",
        institution: Optional[str] = None,
        currency: Optional[str] = None,
        external_id: Optional[str] = None,
        actor_id: Optional[str] = None,
    ) -> dict[str, Any]:
        portfolio = self._require_portfolio(portfolio_id, actor_id)
        clean_name = str(name or "").strip()
        if not clean_name:
            raise PortfolioError(INVALID_ACCOUNT, "`name` is required", {})
        external = str(external_id).strip() if external_id else None
        if external:
            for existing in self.store.list_accounts(portfolio_id):
                if existing.get("externalId") == external:
                    raise PortfolioError(
                        ACCOUNT_EXISTS,
                        f"account external id {external!r} is already in this portfolio",
                        {"externalId": external, "accountId": existing["id"]},
                    )
        account = self.store.create_account(
            new_id("acct"), portfolio_id, clean_name,
            kind=str(kind or "brokerage"),
            institution=institution,
            currency=(currency or portfolio.get("baseCurrency") or "USD").upper(),
            external_id=external,
        )
        self.store.append_audit(
            new_id("audit"), portfolio_id, "ACCOUNT_CREATED", actor=actor_id,
            after={"accountId": account["id"], "name": account["name"],
                   "currency": account["currency"]},
        )
        return account

    def list_accounts(self, portfolio_id: str, *,
                      actor_id: Optional[str] = None) -> list[dict[str, Any]]:
        self._require_portfolio(portfolio_id, actor_id)
        return self.store.list_accounts(portfolio_id)

    def update_account(
        self,
        portfolio_id: str,
        account_id: str,
        fields: dict[str, Any],
        *,
        actor_id: Optional[str] = None,
    ) -> dict[str, Any]:
        self._require_portfolio(portfolio_id, actor_id)
        existing = self._require_account(portfolio_id, account_id)
        external = fields.get("external_id", fields.get("externalId", existing.get("externalId")))
        external = str(external).strip() if external else None
        if external:
            for other in self.store.list_accounts(portfolio_id):
                if other["id"] != account_id and other.get("externalId") == external:
                    raise PortfolioError(
                        ACCOUNT_EXISTS,
                        f"account external id {external!r} is already in this portfolio",
                        {"externalId": external, "accountId": other["id"]},
                    )
        name = str(fields.get("name") or existing["name"]).strip()
        if not name:
            raise PortfolioError(INVALID_ACCOUNT, "`name` cannot be empty", {})
        updated = self.store.create_account(
            account_id, portfolio_id, name,
            kind=str(fields.get("kind") or existing.get("kind") or "brokerage"),
            institution=fields.get("institution", existing.get("institution")),
            currency=str(fields.get("currency") or existing.get("currency") or "USD").upper(),
            external_id=external,
        )
        self.store.append_audit(
            new_id("audit"), portfolio_id, "ACCOUNT_UPDATED", actor=actor_id,
            before={"name": existing.get("name"), "kind": existing.get("kind")},
            after={"accountId": account_id, "name": updated["name"], "kind": updated["kind"]},
        )
        return updated

    def delete_account(self, portfolio_id: str, account_id: str, *,
                       actor_id: Optional[str] = None) -> dict[str, Any]:
        self._require_portfolio(portfolio_id, actor_id)
        account = self._require_account(portfolio_id, account_id)
        in_use = [
            tx.id for tx in self.store.list_transactions(portfolio_id)
            if tx.account_id == account_id
        ]
        if in_use:
            raise PortfolioError(
                ACCOUNT_IN_USE,
                "this account has transactions; reassign or delete them first",
                {"accountId": account_id, "transactions": len(in_use)},
            )
        removed = self.store.delete_account(portfolio_id, account_id)
        self.store.append_audit(
            new_id("audit"), portfolio_id, "ACCOUNT_DELETED", actor=actor_id,
            before={"accountId": account_id, "name": account.get("name")},
            after={"removed": removed},
        )
        return {"deleted": removed, "accountId": account_id}

    def list_portfolios(self, owner_id: Optional[str] = None) -> list[dict[str, Any]]:
        return self.store.list_portfolios(owner_id)

    def audit_log(self, portfolio_id: str, *, limit: int = 200,
                  actor_id: Optional[str] = None) -> list[dict[str, Any]]:
        self._require_portfolio(portfolio_id, actor_id)
        return self.store.list_audit(portfolio_id, limit=limit)

    def verify_audit(self, portfolio_id: str, *,
                     actor_id: Optional[str] = None) -> dict[str, Any]:
        """Prove the portfolio audit trail has not been edited or reordered.

        The same SHA-256 chain rule as the General Ledger; a broken chain names
        the first bad event instead of returning a bare boolean.
        """
        self._require_portfolio(portfolio_id, actor_id)
        return self.store.verify_audit(portfolio_id)

    def get_portfolio_record(self, portfolio_id: str,
                             actor_id: Optional[str] = None) -> Optional[dict[str, Any]]:
        record = self.store.get_portfolio(portfolio_id)
        if record is not None:
            self._authorize(record, actor_id)
        return record

    def delete_portfolio(self, portfolio_id: str, *, actor_id: Optional[str] = None) -> bool:
        self._require_portfolio(portfolio_id, actor_id)
        self._assert_unlocked(portfolio_id, "delete")
        return self.store.delete_portfolio(portfolio_id)

    def _require_portfolio(self, portfolio_id: str, actor_id: Optional[str] = None) -> dict[str, Any]:
        record = self.store.get_portfolio(portfolio_id)
        if record is None:
            raise PortfolioError(PORTFOLIO_NOT_FOUND, f"portfolio {portfolio_id} not found",
                                 {"portfolioId": portfolio_id})
        self._authorize(record, actor_id)
        return record

    @staticmethod
    def _authorize(record: dict[str, Any], actor_id: Optional[str]) -> None:
        """Tenant boundary: an owned portfolio is readable only by its owner.

        When an `actor_id` is asserted and it does not match the row's `ownerId`,
        access is refused. When no actor is asserted the app is in its documented
        single-user local mode (no authentication) and the row is accessible; an
        Unowned row is a legacy/guest portfolio and never blocks.
        """
        owner = record.get("ownerId")
        if owner and actor_id and str(owner) != str(actor_id):
            raise PortfolioError(
                PORTFOLIO_FORBIDDEN,
                "this portfolio belongs to another user",
                {"portfolioId": record.get("id")},
            )

    def _method(self, portfolio: dict[str, Any], override: Optional[str] = None) -> CostBasisMethod:
        raw = (override or portfolio.get("costBasisMethod") or "FIFO").upper()
        try:
            return CostBasisMethod(raw)
        except ValueError:
            return CostBasisMethod.FIFO

    # ── Mutations ────────────────────────────────────────────────────────────
    def add_transaction(self, portfolio_id: str, payload: dict[str, Any],
                        *, actor_id: Optional[str] = None) -> dict[str, Any]:
        """Validate, idempotently record and account a single transaction."""
        try:
            portfolio = self._require_portfolio(portfolio_id, actor_id)
        except PortfolioError as exc:
            return _failed(exc)
        # Checked after existence/ownership so a missing/foreign portfolio keeps
        # its 404/403 semantics instead of leaking a lock state.
        self._assert_unlocked(portfolio_id, "write")
        if actor_id and not payload.get("created_by"):
            payload = {**payload, "created_by": actor_id}
        built = self._build_transaction(portfolio_id, payload)
        if isinstance(built, PortfolioError):
            return _failed(built)
        tx = built
        warnings: list[str] = []
        if tx.account_id:
            try:
                self._require_account(portfolio_id, tx.account_id)
            except PortfolioError as exc:
                return _failed(exc)

        validation = self._validate(tx, portfolio)
        if validation is not None:
            return _failed(validation)
        if tx.symbol and tx.transaction_type in (TransactionType.BUY, TransactionType.SELL) and tx.price is None:
            warnings.append(f"{tx.transaction_type.value} {tx.symbol} recorded without a price")

        # Pre-check a disposal against the current ledger BEFORE writing, so an
        # oversell can never corrupt the books (validated, then inserted).
        is_disposal = tx.transaction_type in DISPOSALS or (
            tx.transaction_type == TransactionType.ADJUSTMENT and tx.quantity < 0
        )
        if tx.symbol and is_disposal:
            method = self._method(portfolio)
            existing = self.store.list_transactions(portfolio_id, symbol=tx.symbol)
            if tx.account_id:
                # An account-scoped disposal can only consume that account's lots;
                # shares in another custody account cannot cover it.
                existing = [row for row in existing if row.account_id == tx.account_id]
            probe = compute_accounting(portfolio_id, tx.symbol, [*existing, tx], method,
                                       currency=tx.currency)
            if probe.has_errors():
                return _failed(probe.errors[0], warnings=warnings)

        stored, duplicate = self.store.append_transaction(tx)
        if duplicate:
            warnings.append("duplicate transaction ignored (external_id already recorded)")
        else:
            self._rebuild_symbol(portfolio_id, stored, portfolio, persist=True)
        self.store.append_audit(
            new_id("audit"), portfolio_id, "TRANSACTION_RECORDED",
            actor=payload.get("created_by"), reason=payload.get("reason"),
            after={"transactionId": stored.id, "type": stored.transaction_type.value,
                   "symbol": stored.symbol, "duplicate": duplicate},
        )
        position = (
            self._position_for(portfolio_id, stored.symbol, portfolio, account_id=stored.account_id)
            if stored.symbol else None
        )
        return {
            "status": "SUCCESS",
            "duplicate": duplicate,
            "transaction": stored.to_dict(),
            "position": position,
            "warnings": warnings,
        }

    def add_position(self, portfolio_id: str, payload: dict[str, Any],
                     *, actor_id: Optional[str] = None) -> dict[str, Any]:
        """`add_position` creates a BUY (or TRANSFER_IN) transaction — never a raw UPDATE."""
        body = dict(payload)
        body.setdefault("type", TransactionType.BUY.value)
        result = self.add_transaction(portfolio_id, body, actor_id=actor_id)
        if result.get("status") == "SUCCESS":
            from ..event_system.emit import publish_event  # noqa: PLC0415

            publish_event("portfolio.position.added", {
                "portfolioId": portfolio_id,
                "symbol": str(body.get("symbol") or "").upper(),
                "duplicate": bool(result.get("duplicate")),
            }, source="portfolio")
        return result

    async def remove_position(self, portfolio_id: str, payload: dict[str, Any],
                              *, actor_id: Optional[str] = None) -> dict[str, Any]:
        """Remove a position by recording an actual SELL disposal (lots are matched)."""
        try:
            portfolio = self._require_portfolio(portfolio_id, actor_id)
        except PortfolioError as exc:
            return _failed(exc)
        symbol = str(payload.get("symbol") or "").upper()
        if not symbol:
            return _failed(PortfolioError(INVALID_TRANSACTION, "symbol is required"))
        account_id = payload.get("account_id") or payload.get("accountId")
        if account_id:
            try:
                self._require_account(portfolio_id, str(account_id))
            except PortfolioError as exc:
                return _failed(exc)
        result = self._symbol_accounting(portfolio_id, symbol, self._method(portfolio),
                                         account_id=account_id)
        qty = result.remaining_qty
        if qty <= 0:
            return _failed(PortfolioError(
                INVALID_TRANSACTION,
                f"no open position in {symbol} to remove",
                {"symbol": symbol},
            ))
        price = D_or_none(payload.get("price"))
        if price is None and payload.get("use_market_price"):
            price = await self._mark_async(symbol, portfolio)
        if price is None:
            return _failed(PortfolioError(
                INVALID_PRICE,
                f"no price supplied to remove {symbol}; a disposal needs a price for realized P&L",
                {"symbol": symbol},
            ))
        outcome = self.add_transaction(portfolio_id, {
            "type": TransactionType.SELL.value,
            "symbol": symbol,
            "quantity": str(qty),
            "price": str(price),
            "currency": result.currency,
            "account_id": account_id,
            "source": payload.get("source", "manual"),
            "created_by": payload.get("created_by"),
            "reason": payload.get("reason"),
            "notes": payload.get("notes"),
        }, actor_id=actor_id)
        if outcome.get("status") == "SUCCESS":
            from ..event_system.emit import publish_event  # noqa: PLC0415

            publish_event("portfolio.position.removed", {
                "portfolioId": portfolio_id, "symbol": symbol,
            }, source="portfolio")
        return outcome

    def update_position(self, portfolio_id: str, payload: dict[str, Any],
                        *, actor_id: Optional[str] = None) -> dict[str, Any]:
        """Correct a position by appending an auditable ADJUSTMENT.

        History is never rewritten. The delta needed to move from the current
        quantity to the target is recorded as an ADJUSTMENT with the required
        `reason`, and an audit event captures before/after.
        """
        try:
            portfolio = self._require_portfolio(portfolio_id, actor_id)
        except PortfolioError as exc:
            return _failed(exc)
        symbol = str(payload.get("symbol") or "").upper()
        if not symbol:
            return _failed(PortfolioError(INVALID_TRANSACTION, "symbol is required"))
        reason = payload.get("reason")
        if not reason:
            return _failed(PortfolioError(
                INVALID_TRANSACTION,
                "a correction requires a reason (who/what/why is preserved in the audit log)",
            ))
        target = D_or_none(payload.get("target_quantity"))
        if target is None:
            return _failed(PortfolioError(INVALID_TRANSACTION, "target_quantity is required"))
        if target < 0:
            return _failed(PortfolioError(NEGATIVE_QUANTITY, "target_quantity cannot be negative"))
        account_id = payload.get("account_id") or payload.get("accountId")
        if account_id:
            try:
                self._require_account(portfolio_id, str(account_id))
            except PortfolioError as exc:
                return _failed(exc)
        result = self._symbol_accounting(portfolio_id, symbol, self._method(portfolio),
                                         account_id=account_id)
        delta = target - result.remaining_qty
        if delta == 0:
            return {"status": "SUCCESS", "changed": False,
                    "position": self._position_for(portfolio_id, symbol, portfolio,
                                                   account_id=account_id)}
        before = {"quantity": str(result.remaining_qty)}
        outcome = self.add_transaction(portfolio_id, {
            "type": TransactionType.ADJUSTMENT.value,
            "symbol": symbol,
            "quantity": str(delta),
            "price": payload.get("price"),
            "currency": result.currency,
            "account_id": account_id,
            "source": payload.get("source", "correction"),
            "created_by": payload.get("created_by"),
            "reason": reason,
            "notes": payload.get("notes"),
        }, actor_id=actor_id)
        self.store.append_audit(
            new_id("audit"), portfolio_id, "POSITION_CORRECTED",
            actor=payload.get("created_by"), reason=reason,
            before=before, after={"quantity": str(target)},
        )
        if outcome.get("status") == "SUCCESS":
            outcome["changed"] = True
            from ..event_system.emit import publish_event  # noqa: PLC0415

            publish_event("portfolio.position.updated", {
                "portfolioId": portfolio_id, "symbol": symbol, "reason": reason,
            }, source="portfolio")
        return outcome

    def set_position_metadata(self, portfolio_id: str, payload: dict[str, Any],
                              *, actor_id: Optional[str] = None) -> dict[str, Any]:
        try:
            self._require_portfolio(portfolio_id, actor_id)
        except PortfolioError as exc:
            return _failed(exc)
        symbol = str(payload.get("symbol") or "").upper()
        if not symbol:
            return _failed(PortfolioError(INVALID_TRANSACTION, "symbol is required"))
        self.store.set_position_meta(
            portfolio_id, symbol, name=payload.get("name"), asset_class=payload.get("asset_class"),
            sector=payload.get("sector"), country=payload.get("country"),
            strategy=payload.get("strategy"), currency=payload.get("currency"),
        )
        return {"status": "SUCCESS", "symbol": symbol}

    # ── Accounting ───────────────────────────────────────────────────────────
    def _symbol_accounting(self, portfolio_id: str, symbol: str,
                           method: CostBasisMethod, currency: Optional[str] = None,
                           account_id: Optional[str] = None) -> AccountingResult:
        txs = self.store.list_transactions(portfolio_id, symbol=symbol)
        if account_id is not None:
            txs = [tx for tx in txs if tx.account_id == account_id]
        cur = currency or (txs[-1].currency if txs else "USD")
        return compute_accounting(portfolio_id, symbol, txs, method, currency=cur)

    def _rebuild_symbol(self, portfolio_id: str, tx: Transaction, portfolio: dict[str, Any],
                        *, persist: bool) -> AccountingResult:
        method = self._method(portfolio)
        result = self._symbol_accounting(portfolio_id, tx.symbol or "", method, currency=tx.currency)
        if persist and tx.symbol:
            self._persist_symbol(portfolio_id, tx.symbol, result)
        return result

    def _persist_symbol(self, portfolio_id: str, symbol: str, result: AccountingResult) -> None:
        """Persist the DERIVED lots/matches and the materialized position row."""
        self.store.replace_accounting(portfolio_id, symbol, result.open_lots, result.matches)
        self.store.upsert_materialized_position(
            portfolio_id, symbol,
            quantity=str(result.remaining_qty),
            cost_basis=money_str(result.remaining_cost_basis) or "0",
            average_cost=(money_str(result.average_cost) if result.average_cost is not None else None),
            realized_pnl=money_str(result.realized_pnl) or "0",
            currency=result.currency,
        )

    def _position_state(self, portfolio_id: str, symbol: str,
                        method: CostBasisMethod,
                        account_id: Optional[str] = None) -> dict[str, Any]:
        """Fast per-symbol state from the materialized cache, self-healing if absent.

        The cache is DERIVED and never authoritative: a missing row is recomputed
        from the ledger and persisted, so a manually-edited/empty cache still reads
        correctly. An account-scoped read bypasses the aggregate cache (there is
        one cache row per portfolio+symbol, not per account).
        """
        if account_id is not None:
            scoped = self._symbol_accounting(portfolio_id, symbol, method,
                                             account_id=account_id)
            return {
                "quantity": scoped.remaining_qty,
                "cost_basis": scoped.remaining_cost_basis,
                "average_cost": scoped.average_cost,
                "realized": scoped.realized_pnl,
                "currency": scoped.currency,
            }
        row = self.store.get_materialized_position(portfolio_id, symbol)
        if row is not None:
            return {
                "quantity": D(row["quantity"]),
                "cost_basis": D(row["costBasis"]),
                "average_cost": (D(row["averageCost"]) if row["averageCost"] not in (None, "") else None),
                "realized": D(row["realizedPnl"]),
                "currency": row["currency"] or "USD",
            }
        result = self._symbol_accounting(portfolio_id, symbol, method)
        self._persist_symbol(portfolio_id, symbol, result)
        return {
            "quantity": result.remaining_qty,
            "cost_basis": result.remaining_cost_basis,
            "average_cost": result.average_cost,
            "realized": result.realized_pnl,
            "currency": result.currency,
        }

    def _account_view(self, portfolio_id: str, account: dict[str, Any],
                      txs: Sequence[Transaction], method: CostBasisMethod,
                      base: str) -> dict[str, Any]:
        cash = ZERO
        net_deposits = ZERO
        dividends = ZERO
        for tx in txs:
            cash += tx.cash_impact()
            if tx.transaction_type == TransactionType.DEPOSIT:
                net_deposits += tx.cash_impact()
            elif tx.transaction_type == TransactionType.WITHDRAWAL:
                net_deposits -= abs(tx.cash_impact())
            elif tx.transaction_type in (TransactionType.DIVIDEND, TransactionType.INTEREST):
                dividends += tx.cash_impact()
        positions = []
        open_lot_count = 0
        for symbol in _symbols(txs):
            symbol_txs = [tx for tx in txs if tx.symbol == symbol]
            result = compute_accounting(
                portfolio_id, symbol, symbol_txs, method,
                currency=(account.get("currency") or base or "USD").upper(),
            )
            open_lot_count += len(result.open_lots)
            if result.remaining_qty > 0:
                positions.append({
                    "symbol": symbol,
                    "quantity": qty_str(result.remaining_qty),
                    "costBasis": money_str(result.remaining_cost_basis),
                    "averageCost": (money_str(result.average_cost)
                                    if result.average_cost is not None else None),
                    "realizedPnl": money_str(result.realized_pnl),
                    "currency": result.currency,
                    "openLots": len(result.open_lots),
                })
        return {
            "id": account.get("id"),
            "name": account.get("name") or "Unassigned",
            "kind": account.get("kind") or "",
            "institution": account.get("institution"),
            "currency": (account.get("currency") or base or "USD").upper(),
            "externalId": account.get("externalId"),
            "synthetic": bool(account.get("synthetic")),
            "cash": money_str(cash),
            "netDeposits": money_str(net_deposits),
            "dividends": money_str(dividends),
            "transactionCount": len(txs),
            "openLots": open_lot_count,
            "positions": positions,
        }

    def _accounts_view(self, portfolio_id: str, accounts: Sequence[dict[str, Any]],
                       txs: Sequence[Transaction], method: CostBasisMethod,
                       base: str) -> tuple[list[dict[str, Any]], Any]:
        groups: dict[str, list[Transaction]] = {account["id"]: [] for account in accounts}
        unassigned: list[Transaction] = []
        for tx in txs:
            if tx.account_id and tx.account_id in groups:
                groups[tx.account_id].append(tx)
            else:
                unassigned.append(tx)
        rows = [
            self._account_view(portfolio_id, account, groups[account["id"]], method, base)
            for account in accounts
        ]
        if unassigned:
            rows.append(self._account_view(
                portfolio_id,
                {"id": None, "name": "Unassigned", "kind": "unassigned",
                 "currency": base, "synthetic": True},
                unassigned, method, base,
            ))
        total = sum((D(row["cash"]) for row in rows), ZERO)
        return rows, total

    def rebuild_all(self, portfolio_id: str, *, actor_id: Optional[str] = None) -> dict[str, Any]:
        """Recompute and persist every symbol's lots/matches and materialized row."""
        try:
            portfolio = self._require_portfolio(portfolio_id, actor_id=actor_id)
        except PortfolioError as exc:
            return _failed(exc)
        method = self._method(portfolio)
        symbols = _symbols(self.store.list_transactions(portfolio_id))
        for symbol in symbols:
            result = self._symbol_accounting(portfolio_id, symbol, method)
            self._persist_symbol(portfolio_id, symbol, result)
        current = set(symbols)
        for stale in set(self.store.list_materialized_positions(portfolio_id)) - current:
            self.store.delete_materialized_position(portfolio_id, stale)
        return {"status": "SUCCESS", "symbols": len(symbols)}

    def _position_for(self, portfolio_id: str, symbol: str, portfolio: dict[str, Any],
                      account_id: Optional[str] = None) -> Optional[dict[str, Any]]:
        result = self._symbol_accounting(portfolio_id, symbol, self._method(portfolio),
                                         account_id=account_id)
        return {
            "symbol": symbol,
            "quantity": qty_str(result.remaining_qty),
            "costBasis": money_str(result.remaining_cost_basis),
            "averageCost": money_str(result.average_cost) if result.average_cost is not None else None,
            "realizedPnl": money_str(result.realized_pnl),
        }

    # ── Read ─────────────────────────────────────────────────────────────────
    async def read(
        self,
        portfolio_id: str,
        *,
        include: Optional[Sequence[str]] = None,
        marks: Optional[dict[str, Any]] = None,
        gateway: Any = None,
        histories: Optional[dict[str, Sequence[float]]] = None,
        benchmark_closes: Optional[Sequence[float]] = None,
        confidence: float = 0.95,
        actor_id: Optional[str] = None,
    ) -> dict[str, Any]:
        portfolio = self._require_portfolio(portfolio_id, actor_id)
        include_set = set(include or ["positions", "cash", "pnl", "allocation", "exposure"])
        base = portfolio.get("baseCurrency") or "USD"
        adapter = MarketAdapter(
            gateway=gateway or self.gateway, base_currency=base, marks=marks or {}
        )
        method = self._method(portfolio)
        meta = self.store.list_position_meta(portfolio_id)

        txs = self.store.list_transactions(portfolio_id)
        symbols: list[str] = []
        cash = ZERO
        net_deposits = ZERO
        dividends = ZERO
        for tx in txs:
            cash += tx.cash_impact()
            if tx.transaction_type == TransactionType.DEPOSIT:
                net_deposits += tx.cash_impact()
            elif tx.transaction_type == TransactionType.WITHDRAWAL:
                net_deposits -= abs(tx.cash_impact())
            elif tx.transaction_type in (TransactionType.DIVIDEND, TransactionType.INTEREST):
                dividends += tx.cash_impact()
            if tx.symbol and tx.transaction_type.value in {t.value for t in (ACQUISITIONS | DISPOSALS)} | {"ADJUSTMENT", "SPLIT", "REVERSE_SPLIT"}:
                if tx.symbol not in symbols:
                    symbols.append(tx.symbol)

        all_positions: list[dict[str, Any]] = []
        missing_fx: list[str] = []

        # Resolve quotes/FX CONCURRENTLY (bounded). A portfolio of N symbols used
        # to pay one provider round-trip after another; a handful of slow symbols
        # exceeded the tool's 20 s ceiling even though each lookup is independent.
        # `gather` keeps the input order, so positions stay deterministic.
        semaphore = asyncio.Semaphore(6)

        async def resolve_position(symbol: str) -> tuple[Any, ...]:
            async with semaphore:
                state = self._position_state(portfolio_id, symbol, method)
                info = meta.get(symbol, {})
                instr_currency = (info.get("currency") or _currency_of(txs, symbol) or base).upper()
                mark = await adapter.mark(symbol)
                fx_currency = (mark.currency or instr_currency).upper()
                fx = (
                    Decimal("1")
                    if fx_currency == base.upper()
                    else await adapter.fx_per_base(fx_currency)
                )
                return symbol, state, info, fx_currency, mark, fx

        resolved = await asyncio.gather(*(resolve_position(s) for s in symbols))
        for symbol, state, info, fx_currency, mark, fx in resolved:
            if fx_currency != base.upper() and fx is None:
                missing_fx.append(f"{base}/{fx_currency}")
            price = mark.price
            quantity = state["quantity"]
            cost_basis_native = state["cost_basis"]
            market_value = (quantity * price) if (price is not None and quantity > 0) else None
            unrealized = (market_value - cost_basis_native) if market_value is not None else None
            realized = state["realized"]
            all_positions.append({
                "symbol": symbol,
                "name": info.get("name") or symbol,
                "assetClass": info.get("assetClass") or "UNCLASSIFIED",
                "sector": info.get("sector") or "UNCLASSIFIED",
                "country": info.get("country") or "UNCLASSIFIED",
                "strategy": info.get("strategy") or "UNCLASSIFIED",
                "currency": fx_currency,
                "quantity": quantity,
                "averageCost": state["average_cost"],
                "costBasis": cost_basis_native,
                "costBasisBase": _to_base(cost_basis_native, fx),
                "price": price,
                "marketValue": market_value,
                "marketValueBase": _to_base(market_value, fx),
                "fxPerBase": fx,
                "unrealizedPnl": unrealized,
                "unrealizedPnlBase": _to_base(unrealized, fx),
                "realizedPnl": realized,
                "realizedPnlBase": _to_base(realized, fx) if realized else ZERO,
                "mark": mark,
            })

        open_positions = [p for p in all_positions if p["quantity"] > 0]
        invested_base = sum(
            (p["marketValueBase"] for p in open_positions if p["marketValueBase"] is not None), ZERO
        )
        for p in open_positions:
            mv = p["marketValueBase"]
            p["weight"] = _pct(mv, invested_base + cash) if mv is not None else None

        val = analytics.valuation(base, open_positions, cash, missing_fx=missing_fx)
        positions = open_positions

        response: dict[str, Any] = {
            "portfolio": {
                "id": portfolio["id"],
                "name": portfolio["name"],
                "baseCurrency": base,
                "costBasisMethod": method.value,
                "status": portfolio.get("status"),
                "kind": portfolio.get("kind") or "portfolio",
                "metadata": _portfolio_metadata(portfolio.get("metadata")),
            },
            "positions": [_serialize_position(p) for p in open_positions],
            "cash": {
                "baseCurrency": base,
                "balance": money_str(cash),
                "netDeposits": money_str(net_deposits),
                "dividends": money_str(dividends),
            },
            "valuation": val,
            "dataQuality": val["dataQuality"],
            "calculationVersion": CALCULATION_VERSION,
            "provenance": _provenance(base, positions),
        }
        account_rows, account_cash_total = self._accounts_view(
            portfolio_id, self.store.list_accounts(portfolio_id), txs, method, base
        )
        response["accounts"] = account_rows
        response["accountCashTotal"] = money_str(account_cash_total)
        response["accountsReconcile"] = account_cash_total == cash
        if "pnl" in include_set:
            response["pnl"] = analytics.pnl(all_positions, cash=cash, net_deposits=net_deposits, dividends=dividends)
        if "allocation" in include_set:
            response["allocation"] = {
                dim: analytics.allocation(base, positions, cash, dim)
                for dim in ("asset", "assetClass", "sector", "currency")
            }
        if "exposure" in include_set:
            response["exposure"] = {
                dim: analytics.exposure(base, positions, cash, dim)
                for dim in ("asset", "assetClass", "sector", "currency", "country")
            }
        if "risk" in include_set:
            weights = {p["symbol"]: float(p["weight"] or 0) / 100.0 for p in positions}
            closes = histories or {}
            if not closes and (gateway or self.gateway):
                closes = await self._fetch_histories(symbols, gateway or self.gateway)
            from .risk import compute_risk

            response["risk"] = compute_risk(
                weights=weights, closes=closes, benchmark_closes=benchmark_closes, confidence=confidence
            )
        if "performance" in include_set:
            snapshots = self.store.list_snapshots(portfolio_id, limit=1000)
            nav, flows = _nav_from_snapshots(snapshots)
            from .performance import compute_performance

            response["performance"] = compute_performance(
                nav=nav, flows=flows, benchmark_nav=benchmark_closes
            )
        return response

    async def _fetch_histories(self, symbols: Sequence[str], gateway: Any) -> dict[str, list[float]]:
        out: dict[str, list[float]] = {}
        for symbol in symbols:
            try:
                series = await gateway.get_historical_prices(symbol, interval="1d", freshness="slow")
            except Exception:  # noqa: BLE001 - missing history is reported, not fabricated
                continue
            if series is None or getattr(series, "code", None) is not None:
                continue
            closes = [b.close for b in (getattr(series, "bars", []) or []) if b.close is not None]
            if len(closes) >= 2:
                out[symbol] = closes
        return out

    # ── Tax lots ─────────────────────────────────────────────────────────────
    def calculate_tax_lots(self, portfolio_id: str, *, symbol: Optional[str] = None,
                           persist: bool = False, account_id: Optional[str] = None,
                           actor_id: Optional[str] = None) -> dict[str, Any]:
        portfolio = self._require_portfolio(portfolio_id, actor_id)
        method = self._method(portfolio)
        txs = self.store.list_transactions(portfolio_id)
        if account_id:
            self._require_account(portfolio_id, str(account_id))
            txs = [tx for tx in txs if tx.account_id == account_id]
        symbols = [symbol] if symbol else _symbols(txs)
        out = []
        for sym in symbols:
            result = self._symbol_accounting(portfolio_id, sym, method,
                                             account_id=account_id)
            # The cache is aggregate (portfolio+symbol); an account-scoped view is
            # a report and must not overwrite it.
            if persist and not account_id:
                self.store.replace_accounting(portfolio_id, sym, result.open_lots, result.matches)
            out.append(result.to_dict())
        return {
            "portfolioId": portfolio_id,
            "accountId": account_id,
            "method": method.value,
            "symbols": out,
            "totalRealizedPnl": money_str(sum((D(row["realizedPnl"]) for row in out), ZERO)),
        }

    def realized_gains(
        self,
        portfolio_id: str,
        *,
        tax_year: Optional[int] = None,
        long_term_days: int = 365,
        wash_sale_window_days: int = 30,
        include_wash_sale: bool = True,
        actor_id: Optional[str] = None,
    ) -> dict[str, Any]:
        """Realized gains by tax year and holding term, with wash-sale candidates.

        This is a report over the engine's lot matches. It does not apply a
        jurisdiction's rule or adjust basis; a wash-sale candidate names the
        replacement purchase it found and leaves the decision to the user.
        """
        portfolio = self._require_portfolio(portfolio_id, actor_id)
        method = self._method(portfolio)
        base = (portfolio.get("baseCurrency") or "USD").upper()
        txs = self.store.list_transactions(portfolio_id)
        symbols = _symbols(txs)
        matches: list[dict[str, Any]] = []
        for symbol in symbols:
            result = self._symbol_accounting(portfolio_id, symbol, method)
            matches.extend(match.to_dict() for match in result.matches)
        acquisitions = [
            {
                "id": tx.id,
                "symbol": tx.symbol,
                "date": tx.effective_date(),
                "quantity": qty_str(tx.quantity),
                "type": tx.transaction_type.value,
            }
            for tx in txs
            if tx.symbol and tx.transaction_type in ACQUISITIONS
        ]
        currencies = {
            symbol: (_currency_of(txs, symbol) or base).upper() for symbol in symbols
        }
        year = None if tax_year in (None, "") else int(tax_year)
        return build_realized_gains_report(
            portfolio_id=portfolio_id,
            method=method.value,
            base_currency=base,
            matches=matches,
            acquisitions=acquisitions,
            currency_by_symbol=currencies,
            tax_year=year,
            long_term_days=long_term_days,
            wash_sale_window_days=wash_sale_window_days,
            include_wash_sale=bool(include_wash_sale),
        )

    # ── Rebalancing ──────────────────────────────────────────────────────────
    async def rebalance(self, portfolio_id: str, payload: dict[str, Any],
                        *, actor_id: Optional[str] = None) -> dict[str, Any]:
        try:
            portfolio = self._require_portfolio(portfolio_id, actor_id)
        except PortfolioError as exc:
            return _failed(exc)
        target = payload.get("target") or payload.get("target_allocation")
        if not isinstance(target, dict) or not target:
            return _failed(PortfolioError(INVALID_TARGET, "a target allocation mapping is required"))
        base = portfolio.get("baseCurrency") or "USD"
        method = self._method(portfolio)
        reading = await self.read(portfolio_id, include=["positions"], marks=payload.get("marks"),
                                  gateway=payload.get("gateway"), actor_id=actor_id)
        base_rows = []
        lots_by_symbol: dict[str, list[dict[str, Any]]] = {}
        for symbol in _symbols(self.store.list_transactions(portfolio_id)):
            res = self._symbol_accounting(portfolio_id, symbol, method)
            lots_by_symbol[symbol.upper()] = [lot.to_dict() for lot in res.open_lots]
        for p in reading["positions"]:
            base_rows.append({
                "symbol": p["symbol"],
                "quantity": D(p["quantity"]),
                "price": D(p["price"]) if p["price"] is not None else None,
                "marketValueBase": D(p["marketValueBase"]) if p["marketValueBase"] is not None else None,
                "fxPerBase": D(p["fxPerBase"]) if p["fxPerBase"] is not None else None,
                "currency": p["currency"],
            })
        from .rebalance import compute_rebalance

        policy = self.store.get_policy(portfolio_id) or {}
        result = compute_rebalance(
            base_currency=base,
            total_value=D(reading["valuation"]["totalValue"]),
            cash=D(reading["cash"]["balance"]),
            positions=base_rows,
            target=target,
            allowed_drift=float(payload.get("allowed_drift", 0.05)),
            method=payload.get("method", policy.get("rebalanceMethod") or "threshold"),
            fee_rate=float(payload.get("fee_rate", 0.0)),
            tax_rules=payload.get("tax_rules", policy.get("taxRules")),
            lots_by_symbol=lots_by_symbol,
            cost_method=method.value,
            min_trade_value=float(payload.get("min_trade_value", 0.0)),
            no_trade=payload.get("no_trade"),
        )
        plan_id = new_id("plan")
        if payload.get("persist", True):
            recs = []
            for rec in result["recommendations"]:
                recs.append({**rec, "id": new_id("rec")})
            self.store.save_rebalance_plan(
                plan_id, portfolio_id, as_of=_now_iso(), method=result["method"],
                status="PROPOSED", target=target, payload=result, recommendations=recs,
                created_by=payload.get("created_by"),
            )
            self.store.append_audit(
                new_id("audit"), portfolio_id, "REBALANCE_PROPOSED",
                actor=payload.get("created_by"), reason="rebalance proposal generated",
                after={"planId": plan_id, "recommendations": len(recs)},
            )
        result["planId"] = plan_id
        result["status"] = "PROPOSED"
        return result

    def list_rebalance_plans(self, portfolio_id: str, limit: int = 50,
                             actor_id: Optional[str] = None) -> list[dict[str, Any]]:
        self._require_portfolio(portfolio_id, actor_id)
        return self.store.list_rebalance_plans(portfolio_id, limit)

    # ── Snapshots ────────────────────────────────────────────────────────────
    async def snapshot(self, portfolio_id: str, *, marks: Optional[dict[str, Any]] = None,
                       gateway: Any = None,                        created_by: Optional[str] = None,
                       actor_id: Optional[str] = None) -> dict[str, Any]:
        try:
            reading = await self.read(portfolio_id, include=["positions", "cash", "pnl"],
                                      marks=marks, gateway=gateway, actor_id=actor_id)
        except PortfolioError as exc:
            return _failed(exc)
        created_by = created_by or actor_id
        snap_id = new_id("snap")
        val = reading["valuation"]
        pnl = reading.get("pnl", {})
        total = D(val["totalValue"])
        cost = D(pnl.get("costBasis", "0"))
        ret = (float(total - cost) / float(cost) * 100) if cost and float(cost) != 0 else None
        self.store.save_snapshot(
            snap_id, portfolio_id, as_of=_now_iso(), base_currency=val["baseCurrency"],
            total_value=val["totalValue"], cash=val["cash"], invested_value=val["investedValue"],
            realized_pnl=pnl.get("realizedPnl"), unrealized_pnl=pnl.get("unrealizedPnl"),
            return_pct=None if ret is None else f"{ret:.6f}",
            data_quality=val["dataQuality"]["status"], payload={
                "positions": reading["positions"], "pnl": pnl,
            }, created_by=created_by,
        )
        return {"status": "SUCCESS", "snapshotId": snap_id, "asOf": _now_iso(),
                "totalValue": val["totalValue"], "dataQuality": val["dataQuality"]}

    def list_snapshots(self, portfolio_id: str, limit: int = 365,
                       actor_id: Optional[str] = None) -> list[dict[str, Any]]:
        self._require_portfolio(portfolio_id, actor_id)
        return self.store.list_snapshots(portfolio_id, limit)

    def history(self, portfolio_id: str, limit: int = 200,
                actor_id: Optional[str] = None) -> dict[str, Any]:
        self._require_portfolio(portfolio_id, actor_id)
        return {
            "snapshots": self.store.list_snapshots(portfolio_id, limit),
            "audit": self.store.list_audit(portfolio_id, limit),
            "transactions": [tx.to_dict() for tx in self.store.list_transactions(portfolio_id)],
        }

    # ── Corporate actions ────────────────────────────────────────────────────
    def apply_corporate_action(self, portfolio_id: str, payload: dict[str, Any],
                               *, actor_id: Optional[str] = None) -> dict[str, Any]:
        """Apply an auditable corporate action as real ledger entries.

        Supported: `SPLIT`, `REVERSE_SPLIT`, `SPIN_OFF`, `MERGER`, `SYMBOL_CHANGE`.
        Each appends normal transactions (never rewrites history) and an audit
        event. Unsupported actions are refused, never faked.
        """
        try:
            portfolio = self._require_portfolio(portfolio_id, actor_id)
        except PortfolioError as exc:
            return _failed(exc)
        action = str(payload.get("action") or "").strip().upper()
        method = self._method(portfolio)
        handlers = {
            "SPLIT": self._action_split,
            "REVERSE_SPLIT": self._action_split,
            "SPIN_OFF": self._action_spin_off,
            "MERGER": self._action_merger,
            "SYMBOL_CHANGE": self._action_symbol_change,
            "RIGHTS_ISSUE": self._action_rights_issue,
            "RETURN_OF_CAPITAL": self._action_return_of_capital,
            "WARRANT_EXERCISE": self._action_warrant_exercise,
        }
        if action not in handlers:
            return _failed(PortfolioError(
                UNSUPPORTED_CORPORATE_ACTION,
                f"unsupported corporate action {action!r}",
                {"supported": sorted(handlers)},
            ))
        try:
            outcome = handlers[action](portfolio, payload, method, actor_id)
        except PortfolioError as exc:
            return _failed(exc)
        if outcome.get("status") == "SUCCESS":
            self.store.append_audit(
                new_id("audit"), portfolio_id, "CORPORATE_ACTION", actor=actor_id,
                reason=payload.get("reason"),
                after={"action": action, **{k: v for k, v in outcome.items() if k not in ("transaction", "transactionIds", "status")}},
            )
        outcome["action"] = action
        return outcome

    def _action_split(self, portfolio: dict[str, Any], payload: dict[str, Any],
                      method: CostBasisMethod, actor_id: Optional[str]) -> dict[str, Any]:
        symbol = str(payload.get("symbol") or "").upper()
        ratio = D_or_none(payload.get("ratio") if "ratio" in payload else payload.get("quantity"))
        if not symbol or ratio is None or ratio <= 0:
            raise PortfolioError(INVALID_TRANSACTION, "a split requires `symbol` and a positive `ratio`")
        return self.add_transaction(
            portfolio["id"],
            {"type": "SPLIT" if str(payload.get("action")).upper() != "REVERSE_SPLIT" else "REVERSE_SPLIT",
             "symbol": symbol, "quantity": str(ratio)},
            actor_id=actor_id,
        )

    def _action_spin_off(self, portfolio: dict[str, Any], payload: dict[str, Any],
                         method: CostBasisMethod, actor_id: Optional[str]) -> dict[str, Any]:
        parent = str(payload.get("parent_symbol") or "").upper()
        child = str(payload.get("child_symbol") or "").upper()
        if not parent or not child or parent == child:
            raise PortfolioError(INVALID_TRANSACTION, "a spin-off requires distinct parent_symbol and child_symbol")
        state = self._position_state(portfolio["id"], parent, method)
        pq = state["quantity"]
        if pq <= 0:
            raise PortfolioError(BASIS_UNAVAILABLE, f"no open position in parent {parent}")
        child_qty = D_or_none(payload.get("child_quantity"))
        if child_qty is None:
            per = D_or_none(payload.get("child_shares_per_parent"))
            if per is None:
                raise PortfolioError(INVALID_TRANSACTION, "provide child_quantity or child_shares_per_parent")
            child_qty = q_qty(per * pq)
        else:
            child_qty = q_qty(child_qty)
        child_price = D_or_none(payload.get("child_price"))
        parent_price = D_or_none(payload.get("parent_price"))
        if child_price is None or parent_price is None:
            raise PortfolioError(INVALID_PRICE, "child_price and parent_price (FMV) are required to allocate basis")
        parent_fmv = pq * parent_price
        child_fmv = child_qty * child_price
        total_fmv = parent_fmv + child_fmv
        if total_fmv <= 0:
            raise PortfolioError(INVALID_AMOUNT, "parent and child FMV cannot both be zero")
        allocated = state["cost_basis"] * (child_fmv / total_fmv)
        currency = state["currency"]
        first = self.add_transaction(portfolio["id"], {
            "type": "SPIN_OFF", "symbol": child, "quantity": str(child_qty),
            "gross_amount": money_str(allocated), "currency": currency,
            "notes": f"spin-off from {parent}", "trade_date": payload.get("trade_date"),
        }, actor_id=actor_id)
        if first.get("status") != "SUCCESS":
            return first
        second = self.add_transaction(portfolio["id"], {
            "type": "COST_BASIS_ADJUSTMENT", "symbol": parent, "quantity": "0",
            "basis_adjustment": money_str(-allocated), "currency": currency,
            "notes": f"basis reallocated to spin-off child {child}",
            "trade_date": payload.get("trade_date"),
        }, actor_id=actor_id)
        status = "SUCCESS" if second.get("status") == "SUCCESS" else second.get("status", "FAILED")
        return {"status": status, "childSymbol": child, "parentSymbol": parent,
                "childQuantity": str(child_qty), "allocatedBasis": money_str(allocated),
                "transactionIds": [first.get("transaction", {}).get("id"), second.get("transaction", {}).get("id")]}

    def _match_cost(self, portfolio_id: str, symbol: str, extra: Transaction,
                    method: CostBasisMethod) -> Decimal:
        existing = self.store.list_transactions(portfolio_id, symbol=symbol)
        before = compute_accounting(portfolio_id, symbol, existing, method)
        after = compute_accounting(portfolio_id, symbol, [*existing, extra], method)
        return after.realized_cost_basis - before.realized_cost_basis

    def _action_merger(self, portfolio: dict[str, Any], payload: dict[str, Any],
                       method: CostBasisMethod, actor_id: Optional[str]) -> dict[str, Any]:
        acquired = str(payload.get("acquired_symbol") or "").upper()
        acquirer = str(payload.get("acquirer_symbol") or "").upper()
        if not acquired or not acquirer or acquired == acquirer:
            raise PortfolioError(INVALID_TRANSACTION, "a merger requires distinct acquired_symbol and acquirer_symbol")
        ratio = D_or_none(payload.get("exchange_ratio"))
        acquirer_price = D_or_none(payload.get("acquirer_price"))
        cash_per_share = D_or_none(payload.get("cash_per_share")) or ZERO
        if ratio is None or ratio < 0 or acquirer_price is None:
            raise PortfolioError(INVALID_TRANSACTION, "exchange_ratio and acquirer_price are required")
        state = self._position_state(portfolio["id"], acquired, method)
        aq = state["quantity"]
        if aq <= 0:
            raise PortfolioError(BASIS_UNAVAILABLE, f"no open position in {acquired}")
        currency = state["currency"]
        abasis = state["cost_basis"]
        acquirer_shares = q_qty(aq * ratio)
        stock_fmv = acquirer_shares * acquirer_price
        cash_total = aq * cash_per_share
        consideration = stock_fmv + cash_total
        if consideration <= 0:
            raise PortfolioError(INVALID_AMOUNT, "merger consideration cannot be zero")
        carried = abasis
        remaining = aq
        tx_ids: list[Optional[str]] = []
        if cash_total > 0:
            fraction = cash_total / consideration
            cash_qty = q_qty(aq * fraction)
            sell_price = (cash_total / cash_qty) if cash_qty > 0 else ZERO
            probe = self._build_transaction(portfolio["id"], {
                "type": "SELL", "symbol": acquired, "quantity": str(cash_qty), "price": money_str(sell_price),
            })
            if isinstance(probe, PortfolioError):
                raise probe
            matched = self._match_cost(portfolio["id"], acquired, probe, method)
            carried = abasis - matched
            if carried < 0:
                carried = ZERO
            cash_leg = self.add_transaction(portfolio["id"], {
                "type": "SELL", "symbol": acquired, "quantity": str(cash_qty), "price": money_str(sell_price),
                "notes": f"cash consideration in merger into {acquirer}",
            }, actor_id=actor_id)
            if cash_leg.get("status") != "SUCCESS":
                return cash_leg
            tx_ids.append(cash_leg.get("transaction", {}).get("id"))
            remaining = q_qty(aq - cash_qty)
        if remaining > 0:
            transfer = self.add_transaction(portfolio["id"], {
                "type": "TRANSFER_OUT", "symbol": acquired, "quantity": str(remaining),
                "notes": f"stock consideration in merger into {acquirer}",
            }, actor_id=actor_id)
            if transfer.get("status") != "SUCCESS":
                return transfer
            tx_ids.append(transfer.get("transaction", {}).get("id"))
        if acquirer_shares > 0:
            acquire = self.add_transaction(portfolio["id"], {
                "type": "MERGER", "symbol": acquirer, "quantity": str(acquirer_shares),
                "gross_amount": money_str(carried), "currency": currency,
                "notes": f"merger from {acquired} (ratio {ratio})",
            }, actor_id=actor_id)
            if acquire.get("status") != "SUCCESS":
                return acquire
            tx_ids.append(acquire.get("transaction", {}).get("id"))
        return {"status": "SUCCESS", "acquiredSymbol": acquired, "acquirerSymbol": acquirer,
                "acquirerShares": str(acquirer_shares), "cashConsideration": money_str(cash_total),
                "carriedBasis": money_str(carried), "transactionIds": tx_ids}

    def _action_symbol_change(self, portfolio: dict[str, Any], payload: dict[str, Any],
                              method: CostBasisMethod, actor_id: Optional[str]) -> dict[str, Any]:
        old = str(payload.get("old_symbol") or "").upper()
        new = str(payload.get("new_symbol") or "").upper()
        if not old or not new or old == new:
            raise PortfolioError(INVALID_TRANSACTION, "symbol change requires distinct old_symbol and new_symbol")
        state = self._position_state(portfolio["id"], old, method)
        q = state["quantity"]
        if q <= 0:
            raise PortfolioError(BASIS_UNAVAILABLE, f"no open position in {old}")
        currency = state["currency"]
        per_unit = state["cost_basis"] / q
        out = self.add_transaction(portfolio["id"], {
            "type": "TRANSFER_OUT", "symbol": old, "quantity": str(q),
            "notes": f"symbol change {old} -> {new}",
        }, actor_id=actor_id)
        if out.get("status") != "SUCCESS":
            return out
        incoming = self.add_transaction(portfolio["id"], {
            "type": "TRANSFER_IN", "symbol": new, "quantity": str(q),
            "price": money_str(per_unit), "currency": currency,
            "notes": f"symbol change from {old}",
        }, actor_id=actor_id)
        if incoming.get("status") != "SUCCESS":
            return incoming
        return {"status": "SUCCESS", "oldSymbol": old, "newSymbol": new,
                "transactionIds": [out.get("transaction", {}).get("id"), incoming.get("transaction", {}).get("id")]}

    def _action_rights_issue(self, portfolio: dict[str, Any], payload: dict[str, Any],
                             method: CostBasisMethod, actor_id: Optional[str]) -> dict[str, Any]:
        symbol = str(payload.get("symbol") or "").upper()
        qty = D_or_none(payload.get("quantity"))
        price = D_or_none(payload.get("price"))
        if not symbol or qty is None or qty <= 0 or price is None:
            raise PortfolioError(INVALID_TRANSACTION, "a rights issue requires symbol, quantity and price")
        return self.add_transaction(portfolio["id"], {
            "type": "RIGHTS_ISSUE", "symbol": symbol, "quantity": str(qty), "price": money_str(price),
            "notes": payload.get("notes") or "rights issue subscription",
        }, actor_id=actor_id)

    def _action_return_of_capital(self, portfolio: dict[str, Any], payload: dict[str, Any],
                                  method: CostBasisMethod, actor_id: Optional[str]) -> dict[str, Any]:
        symbol = str(payload.get("symbol") or "").upper()
        if not symbol:
            raise PortfolioError(INVALID_TRANSACTION, "a return of capital requires a symbol")
        state = self._position_state(portfolio["id"], symbol, method)
        q = state["quantity"]
        if q <= 0:
            raise PortfolioError(BASIS_UNAVAILABLE, f"no open position in {symbol}")
        total = D_or_none(payload.get("total_amount") or payload.get("totalAmount"))
        if total is None:
            per_share = D_or_none(payload.get("amount_per_share") or payload.get("amountPerShare"))
            if per_share is None:
                raise PortfolioError(INVALID_TRANSACTION, "provide total_amount or amount_per_share")
            total = per_share * q
        if total <= 0:
            raise PortfolioError(INVALID_AMOUNT, "return of capital must be positive")
        basis = state["cost_basis"]
        reduce = total if total <= basis else basis
        excess = total - reduce
        first = self.add_transaction(portfolio["id"], {
            "type": "RETURN_OF_CAPITAL", "symbol": symbol, "quantity": "0",
            "basis_adjustment": money_str(-reduce), "net_amount": money_str(total),
            "currency": state["currency"], "notes": "non-taxable return of capital",
        }, actor_id=actor_id)
        if first.get("status") != "SUCCESS":
            return first
        tx_ids = [first.get("transaction", {}).get("id")]
        if excess > 0:
            second = self.add_transaction(portfolio["id"], {
                "type": "REALIZED_ADJUSTMENT", "symbol": symbol, "quantity": "0",
                "net_amount": money_str(excess), "currency": state["currency"],
                "notes": "return of capital exceeded basis — taxable gain",
            }, actor_id=actor_id)
            if second.get("status") != "SUCCESS":
                return second
            tx_ids.append(second.get("transaction", {}).get("id"))
        return {"status": "SUCCESS", "symbol": symbol, "returned": money_str(total),
                "basisReduction": money_str(reduce), "taxableGain": money_str(excess),
                "transactionIds": tx_ids}

    def _action_warrant_exercise(self, portfolio: dict[str, Any], payload: dict[str, Any],
                                 method: CostBasisMethod, actor_id: Optional[str]) -> dict[str, Any]:
        warrant = str(payload.get("warrant_symbol") or "").upper()
        underlying = str(payload.get("underlying_symbol") or "").upper()
        if not warrant or not underlying or warrant == underlying:
            raise PortfolioError(INVALID_TRANSACTION, "provide distinct warrant_symbol and underlying_symbol")
        n = D_or_none(payload.get("quantity"))
        if n is None or n <= 0:
            raise PortfolioError(INVALID_TRANSACTION, "quantity (warrants exercised) is required")
        ratio = D_or_none(payload.get("ratio")) or Decimal("1")
        strike = D_or_none(payload.get("strike")) or ZERO
        state = self._position_state(portfolio["id"], warrant, method)
        if state["quantity"] < n:
            raise PortfolioError(BASIS_UNAVAILABLE, f"only {state['quantity']} {warrant} held, cannot exercise {n}")
        shares = q_qty(n * ratio)
        probe = self._build_transaction(portfolio["id"], {
            "type": "TRANSFER_OUT", "symbol": warrant, "quantity": str(n),
        })
        if isinstance(probe, PortfolioError):
            raise probe
        warrant_cost = self._match_cost(portfolio["id"], warrant, probe, method)
        cash_paid = shares * strike
        total_basis = warrant_cost + cash_paid
        currency = state["currency"]
        out = self.add_transaction(portfolio["id"], {
            "type": "TRANSFER_OUT", "symbol": warrant, "quantity": str(n),
            "notes": f"warrant exercise into {underlying}",
        }, actor_id=actor_id)
        if out.get("status") != "SUCCESS":
            return out
        acquired = self.add_transaction(portfolio["id"], {
            "type": "WARRANT_EXERCISE", "symbol": underlying, "quantity": str(shares),
            "gross_amount": money_str(total_basis), "currency": currency,
            "notes": f"exercised {n} {warrant} at strike {strike}",
        }, actor_id=actor_id)
        if acquired.get("status") != "SUCCESS":
            return acquired
        tx_ids = [out.get("transaction", {}).get("id"), acquired.get("transaction", {}).get("id")]
        if cash_paid > 0:
            cash = self.add_transaction(portfolio["id"], {
                "type": "WITHDRAWAL", "gross_amount": money_str(cash_paid),
                "notes": f"warrant exercise strike for {underlying}",
            }, actor_id=actor_id)
            if cash.get("status") == "SUCCESS":
                tx_ids.append(cash.get("transaction", {}).get("id"))
        return {"status": "SUCCESS", "warrantSymbol": warrant, "underlyingSymbol": underlying,
                "underlyingShares": str(shares), "carriedBasis": money_str(total_basis),
                "cashPaid": money_str(cash_paid), "transactionIds": tx_ids}

    # ── Crypto: swap / bridge / staking ──────────────────────────────────────
    def apply_swap(self, portfolio_id: str, payload: dict[str, Any],
                   *, actor_id: Optional[str] = None) -> dict[str, Any]:
        """A crypto swap = a taxable disposal of A plus an acquisition of B at FMV."""
        try:
            portfolio = self._require_portfolio(portfolio_id, actor_id)
            return self._apply_swap(portfolio, payload, actor_id)
        except PortfolioError as exc:
            return _failed(exc)

    def _apply_swap(self, portfolio: dict[str, Any], payload: dict[str, Any],
                    actor_id: Optional[str]) -> dict[str, Any]:
        frm = str(payload.get("from_symbol") or "").upper()
        to = str(payload.get("to_symbol") or "").upper()
        if not frm or not to or frm == to:
            raise PortfolioError(INVALID_TRANSACTION, "a swap requires distinct from_symbol and to_symbol")
        fq = D_or_none(payload.get("from_quantity"))
        fprice = D_or_none(payload.get("from_price"))
        if fq is None or fq <= 0 or fprice is None:
            raise PortfolioError(INVALID_TRANSACTION, "from_quantity and from_price are required")
        fees = D_or_none(payload.get("fees")) or ZERO
        net_proceeds = fq * fprice - fees
        tq = D_or_none(payload.get("to_quantity"))
        tprice = D_or_none(payload.get("to_price"))
        if tq is None:
            if tprice is None or tprice <= 0:
                raise PortfolioError(INVALID_TRANSACTION, "provide to_quantity or a positive to_price")
            tq = q_qty(net_proceeds / tprice)
        else:
            tq = q_qty(tq)
        if tq <= 0:
            raise PortfolioError(INVALID_TRANSACTION, "computed to_quantity is not positive")
        disposal = self.add_transaction(portfolio["id"], {
            "type": "SWAP", "symbol": frm, "quantity": str(fq), "price": money_str(fprice),
            "fees": money_str(fees), "notes": f"swap into {to}",
        }, actor_id=actor_id)
        if disposal.get("status") != "SUCCESS":
            return disposal
        acquisition = self.add_transaction(portfolio["id"], {
            "type": "TRANSFER_IN", "symbol": to, "quantity": str(tq),
            "gross_amount": money_str(net_proceeds), "currency": (
                payload.get("to_currency") or payload.get("currency") or "USD"),
            "notes": f"swap from {frm}",
        }, actor_id=actor_id)
        status = "SUCCESS" if acquisition.get("status") == "SUCCESS" else acquisition.get("status", "FAILED")
        return {"status": status, "fromSymbol": frm, "toSymbol": to,
                "toQuantity": str(tq), "carriedBasis": money_str(net_proceeds),
                "transactionIds": [disposal.get("transaction", {}).get("id"),
                                   acquisition.get("transaction", {}).get("id")]}

    def apply_bridge(self, portfolio_id: str, payload: dict[str, Any],
                     *, actor_id: Optional[str] = None) -> dict[str, Any]:
        """Bridge an asset across chains: basis carries, no gain/loss."""
        try:
            portfolio = self._require_portfolio(portfolio_id, actor_id)
            method = self._method(portfolio)
            return self._apply_bridge(portfolio, payload, method, actor_id)
        except PortfolioError as exc:
            return _failed(exc)

    def _apply_bridge(self, portfolio: dict[str, Any], payload: dict[str, Any],
                      method: CostBasisMethod, actor_id: Optional[str]) -> dict[str, Any]:
        frm = str(payload.get("from_symbol") or "").upper()
        to = str(payload.get("to_symbol") or "").upper()
        if not frm or not to:
            raise PortfolioError(INVALID_TRANSACTION, "a bridge requires from_symbol and to_symbol")
        q = D_or_none(payload.get("quantity"))
        if q is None or q <= 0:
            raise PortfolioError(INVALID_TRANSACTION, "quantity is required")
        currency = self._position_state(portfolio["id"], frm, method)["currency"]
        probe = self._build_transaction(portfolio["id"], {
            "type": "TRANSFER_OUT", "symbol": frm, "quantity": str(q),
        })
        if isinstance(probe, PortfolioError):
            raise probe
        carried = self._match_cost(portfolio["id"], frm, probe, method)
        tq = q_qty(D_or_none(payload.get("to_quantity")) or q)
        if tq <= 0:
            raise PortfolioError(INVALID_TRANSACTION, "to_quantity must be positive")
        out = self.add_transaction(portfolio["id"], {
            "type": "TRANSFER_OUT", "symbol": frm, "quantity": str(q),
            "notes": f"bridge to {to}",
        }, actor_id=actor_id)
        if out.get("status") != "SUCCESS":
            return out
        incoming = self.add_transaction(portfolio["id"], {
            "type": "TRANSFER_IN", "symbol": to, "quantity": str(tq),
            "price": money_str(carried / tq), "currency": currency,
            "notes": f"bridge from {frm}",
        }, actor_id=actor_id)
        status = "SUCCESS" if incoming.get("status") == "SUCCESS" else incoming.get("status", "FAILED")
        return {"status": status, "fromSymbol": frm, "toSymbol": to, "toQuantity": str(tq),
                "carriedBasis": money_str(carried),
                "transactionIds": [out.get("transaction", {}).get("id"),
                                   incoming.get("transaction", {}).get("id")]}

    def record_reward(self, portfolio_id: str, payload: dict[str, Any],
                      *, actor_id: Optional[str] = None) -> dict[str, Any]:
        """Record a staking reward / airdrop as an acquisition at fair market value."""
        try:
            self._require_portfolio(portfolio_id, actor_id)
        except PortfolioError as exc:
            return _failed(exc)
        kind = str(payload.get("type") or "STAKING_REWARD").upper()
        if kind not in ("STAKING_REWARD", "AIRDROP"):
            return _failed(PortfolioError(INVALID_TRANSACTION, "reward type must be STAKING_REWARD or AIRDROP"))
        return self.add_transaction(portfolio_id, {
            "type": kind, "symbol": payload.get("symbol"), "quantity": payload.get("quantity"),
            "price": payload.get("price"), "currency": payload.get("currency"),
            "trade_date": payload.get("trade_date"), "notes": payload.get("notes"),
        }, actor_id=actor_id)

    def apply_staking(self, portfolio_id: str, payload: dict[str, Any],
                      *, actor_id: Optional[str] = None) -> dict[str, Any]:
        """Lock/unlock staked tokens: audited custody events that never move basis."""
        try:
            self._require_portfolio(portfolio_id, actor_id)
        except PortfolioError as exc:
            return _failed(exc)
        action = str(payload.get("action") or "stake").lower()
        if action not in ("stake", "unstake"):
            return _failed(PortfolioError(INVALID_TRANSACTION, "staking action must be stake or unstake"))
        return self.add_transaction(portfolio_id, {
            "type": "STAKE" if action == "stake" else "UNSTAKE",
            "symbol": payload.get("symbol"), "quantity": payload.get("quantity"),
            "notes": payload.get("notes") or f"{action} custody event",
        }, actor_id=actor_id)

    def apply_slashing(self, portfolio_id: str, payload: dict[str, Any],
                       *, actor_id: Optional[str] = None) -> dict[str, Any]:
        """Write off slashed tokens: a disposal at zero proceeds (realized loss)."""
        try:
            self._require_portfolio(portfolio_id, actor_id)
        except PortfolioError as exc:
            return _failed(exc)
        qty = D_or_none(payload.get("quantity"))
        if qty is None or qty <= 0:
            return _failed(PortfolioError(INVALID_TRANSACTION, "quantity is required"))
        return self.add_transaction(portfolio_id, {
            "type": "SLASHING", "symbol": payload.get("symbol"), "quantity": str(qty),
            "notes": payload.get("notes") or "validator slashing / write-off",
            "trade_date": payload.get("trade_date"),
        }, actor_id=actor_id)

    def record_funding(self, portfolio_id: str, payload: dict[str, Any],
                       *, actor_id: Optional[str] = None) -> dict[str, Any]:
        """Record a perpetual-futures funding payment (signed: + received, - paid)."""
        try:
            self._require_portfolio(portfolio_id, actor_id)
        except PortfolioError as exc:
            return _failed(exc)
        amount = D_or_none(payload.get("amount") if "amount" in payload else payload.get("net_amount"))
        if amount is None:
            return _failed(PortfolioError(INVALID_AMOUNT, "a signed `amount` is required"))
        return self.add_transaction(portfolio_id, {
            "type": "FUNDING", "symbol": payload.get("symbol"), "net_amount": money_str(amount),
            "currency": payload.get("currency"), "trade_date": payload.get("trade_date"),
            "notes": payload.get("notes") or "perpetual funding",
        }, actor_id=actor_id)

    # ── Reconciliation ───────────────────────────────────────────────────────
    def reconcile(self, portfolio_id: str, payload: dict[str, Any],
                  *, actor_id: Optional[str] = None) -> dict[str, Any]:
        """Compare an external statement to the ledger. Never mutates it."""
        try:
            self._require_portfolio(portfolio_id, actor_id)
        except PortfolioError as exc:
            return _failed(exc)
        external = payload.get("external_positions") or payload.get("externalPositions") or []
        if not isinstance(external, list):
            return _failed(PortfolioError(INVALID_TRANSACTION, "external_positions must be a list"))
        internal = [
            {"symbol": s, "quantity": str(row["quantity"])}
            for s, row in self.store.list_materialized_positions(portfolio_id).items()
            if D(row["quantity"]) != 0
        ]
        from .reconciliation import reconcile as _reconcile

        result = _reconcile(
            internal, external,
            tolerance=payload.get("tolerance"),
            relative_tolerance=float(payload.get("relative_tolerance") or 0.0),
        )
        recon_id = new_id("recon")
        self.store.save_reconciliation(
            recon_id, portfolio_id, source=payload.get("source"),
            as_of=payload.get("as_of") or _now_iso(), status=result["status"],
            payload={"external": external, "result": result}, created_by=actor_id,
        )
        self.store.append_audit(
            new_id("audit"), portfolio_id, "RECONCILIATION", actor=actor_id,
            reason=payload.get("source"), after={"status": result["status"], "reconciliationId": recon_id},
        )
        result["reconciliationId"] = recon_id
        return result

    def list_reconciliations(self, portfolio_id: str, limit: int = 50,
                             actor_id: Optional[str] = None) -> list[dict[str, Any]]:
        self._require_portfolio(portfolio_id, actor_id)
        return self.store.list_reconciliations(portfolio_id, limit)

    # ── Optimization / attribution / exposure / hedging / aggregation ────────
    def _weight_map(self, positions: list[dict[str, Any]]) -> dict[str, float]:
        total = ZERO
        for p in positions:
            if p.get("marketValueBase") is not None:
                total += abs(p["marketValueBase"])
        if total == 0:
            return {p["symbol"]: 0.0 for p in positions}
        return {
            p["symbol"]: float((abs(p["marketValueBase"]) if p.get("marketValueBase") is not None else ZERO) / total)
            for p in positions
        }

    def optimize(self, portfolio_id: str, payload: dict[str, Any],
                 *, actor_id: Optional[str] = None) -> dict[str, Any]:
        """Constrained min-variance / mean-variance allocator (proposal only)."""
        try:
            portfolio = self._require_portfolio(portfolio_id, actor_id)
        except PortfolioError as exc:
            return _failed(exc)
        symbols = payload.get("symbols")
        current: dict[str, float] = {}
        if not symbols:
            rows = self.store.list_materialized_positions(portfolio_id)
            open_rows = {s: r for s, r in rows.items() if D(r["quantity"]) != 0}
            symbols = list(open_rows)
            current = _normalize_weights({s: abs(float(D(open_rows[s]["costBasis"]))) for s in symbols})
        from .optimizer import optimize as _optimize

        result = _optimize(
            list(symbols),
            expected_returns=payload.get("expected_returns") or payload.get("expectedReturns"),
            covariance=payload.get("covariance"),
            current_weights=payload.get("current_weights") or (
                [current.get(s, 0.0) for s in symbols] if current else None
            ),
            method=payload.get("method", "min_variance"),
            risk_aversion=float(payload.get("risk_aversion") or 1.0),
            min_weights=payload.get("min_weights") or payload.get("minWeights"),
            max_weights=payload.get("max_weights") or payload.get("maxWeights"),
            turnover_cap=(float(payload["turnover_cap"]) if payload.get("turnover_cap") is not None else None),
        )
        result["portfolioId"] = portfolio["id"]
        result["execution"] = "proposal-only: no trades are placed; requires user approval"
        if result.get("available") and payload.get("persist", True):
            plan_id = new_id("plan")
            recs = []
            weights = result.get("weights", {})
            for symbol, weight in weights.items():
                recs.append({
                    "id": new_id("rec"), "symbol": symbol,
                    "action": "TARGET", "quantity": None, "price": None,
                    "estimatedValue": None, "estFees": None, "taxImpact": None,
                    "estRealizedPnl": None, "reason": f"optimizer target weight {round(weight * 100, 4)}%",
                })
            self.store.save_rebalance_plan(
                plan_id, portfolio_id, as_of=_now_iso(), method=f"optimizer:{result.get('method')}",
                status="PROPOSED", target=weights, payload=result, recommendations=recs, created_by=actor_id,
            )
            result["planId"] = plan_id
        return result

    async def construct(self, payload: dict[str, Any], *,
                        actor_id: Optional[str] = None) -> dict[str, Any]:
        """Portfolio construction & risk engine (§11–§22) — proposal only.

        Thin delegate; the data-prep/estimate/dispatch/persist flow lives in
        `construct_service.construct_payload` so this facade stays within budget.
        """
        from .construct_service import construct_payload

        return await construct_payload(self, payload, actor_id=actor_id)

    async def attribution(self, portfolio_id: str, payload: dict[str, Any],
                          *, actor_id: Optional[str] = None) -> dict[str, Any]:
        from .attribution import brinson_attribution

        segments = payload.get("segments")
        if not segments:
            reading = await self.read(portfolio_id, include=["positions"],
                                      marks=payload.get("marks"), actor_id=actor_id)
            dimension = payload.get("dimension") or "sector"
            benchmark_weights = payload.get("benchmark_weights") or payload.get("benchmarkWeights") or {}
            portfolio_returns = payload.get("portfolio_returns") or payload.get("portfolioReturns") or {}
            benchmark_returns = payload.get("benchmark_returns") or payload.get("benchmarkReturns") or {}
            weights = analytics.allocation(
                reading["valuation"]["baseCurrency"],
                [{"symbol": p["symbol"], "assetClass": p["assetClass"], "sector": p["sector"],
                  "strategy": p["strategy"], "country": p["country"],
                  "marketValueBase": D(p["marketValueBase"]) if p["marketValueBase"] is not None else None}
                 for p in reading["positions"]],
                ZERO, dimension,
            )
            segments = []
            for bucket in weights["breakdown"]:
                key = bucket["key"]
                segments.append({
                    "group": key,
                    "portfolioWeight": (bucket["weight"] or 0.0) / 100.0,
                    "benchmarkWeight": float(benchmark_weights.get(key, 0.0)),
                    "portfolioReturn": float(portfolio_returns.get(key, 0.0)),
                    "benchmarkReturn": float(benchmark_returns.get(key, 0.0)),
                })
        result = brinson_attribution(segments, method=payload.get("method", "fachler"),
                                     benchmark_total_return=payload.get("benchmark_total_return"))
        result["portfolioId"] = portfolio_id
        return result

    async def factor_exposure(self, portfolio_id: str, payload: dict[str, Any],
                              *, actor_id: Optional[str] = None) -> dict[str, Any]:
        reading = await self.read(portfolio_id, include=["positions"], marks=payload.get("marks"),
                                  actor_id=actor_id)
        weights = self._weight_map([
            {"symbol": p["symbol"], "marketValueBase": D(p["marketValueBase"]) if p["marketValueBase"] is not None else None}
            for p in reading["positions"]
        ])
        loadings = payload.get("loadings") or {}
        from .risk import factor_exposure as _fx

        result = _fx(weights, loadings)
        result["portfolioId"] = portfolio_id
        return result

    async def hedge(self, portfolio_id: str, payload: dict[str, Any],
                    *, actor_id: Optional[str] = None) -> dict[str, Any]:
        """Propose FX hedges to flatten non-base currency exposure (proposal only)."""
        reading = await self.read(portfolio_id, include=["positions", "cash"], marks=payload.get("marks"),
                                  actor_id=actor_id)
        base = reading["portfolio"]["baseCurrency"]
        by_currency: dict[str, Decimal] = {}
        for p in reading["positions"]:
            mv = p["marketValueBase"]
            if mv is None:
                continue
            cur = (p["currency"] or base).upper()
            if cur != base.upper():
                by_currency[cur] = by_currency.get(cur, ZERO) + D(mv)
        target_flat = payload.get("target_flat", True)
        target_weights = payload.get("target_currency_weights") or {}
        total = D(reading["valuation"]["totalValue"])
        proposals = []
        for cur, exposure in sorted(by_currency.items()):
            target_value = ZERO if target_flat else total * Decimal(str(target_weights.get(cur, 0.0)))
            gap = exposure - target_value
            proposals.append({
                "currency": cur,
                "exposureBase": money_str(exposure),
                "targetBase": money_str(target_value),
                "hedgeNotionalBase": money_str(gap),
                "direction": "SELL" if gap > 0 else ("BUY" if gap < 0 else "NONE"),
                "note": "an FX hedge instrument (e.g. a forward) offsets this exposure",
            })
        return {
            "portfolioId": portfolio_id, "baseCurrency": base, "proposals": proposals,
            "execution": "proposal-only: hedging instruments must be added or traded with user approval",
        }

    async def aggregate(self, payload: dict[str, Any], *, actor_id: Optional[str] = None) -> dict[str, Any]:
        """Aggregate several portfolios into one view (marks shared)."""
        ids = payload.get("portfolio_ids") or payload.get("portfolioIds")
        if not ids:
            ids = [p["id"] for p in self.store.list_portfolios(payload.get("owner_id") or actor_id)]
        marks = payload.get("marks")
        base = payload.get("base_currency") or "USD"
        by_symbol: dict[str, Decimal] = {}
        totals = {"invested": ZERO, "cash": ZERO}
        by_class: dict[str, Decimal] = {}
        portfolios: list[dict[str, Any]] = []
        for pid in ids:
            try:
                reading = await self.read(pid, include=["positions", "cash"], marks=marks, actor_id=actor_id)
            except PortfolioError:
                continue
            base = reading["portfolio"]["baseCurrency"]
            totals["invested"] += D(reading["valuation"]["investedValue"])
            totals["cash"] += D(reading["cash"]["balance"])
            for p in reading["positions"]:
                if p["marketValueBase"] is None:
                    continue
                by_symbol[p["symbol"]] = by_symbol.get(p["symbol"], ZERO) + D(p["marketValueBase"])
                cls = p["assetClass"] or "UNCLASSIFIED"
                by_class[cls] = by_class.get(cls, ZERO) + D(p["marketValueBase"])
            portfolios.append({"id": pid, "name": reading["portfolio"]["name"],
                               "totalValue": reading["valuation"]["totalValue"]})
        total = totals["invested"] + totals["cash"]
        return {
            "baseCurrency": base,
            "portfolioCount": len(portfolios),
            "totalValue": money_str(total),
            "investedValue": money_str(totals["invested"]),
            "cash": money_str(totals["cash"]),
            "bySymbol": [{"symbol": s, "value": money_str(v)} for s, v in
                         sorted(by_symbol.items(), key=lambda kv: abs(kv[1]), reverse=True)],
            "byAssetClass": [{"key": k, "value": money_str(v)} for k, v in
                             sorted(by_class.items(), key=lambda kv: abs(kv[1]), reverse=True)],
            "portfolios": portfolios,
        }

    # ── Validation / construction ────────────────────────────────────────────
    def _build_transaction(self, portfolio_id: str, payload: dict[str, Any]) -> Transaction | PortfolioError:
        raw_type = payload.get("type") or payload.get("transaction_type")
        if not raw_type:
            return PortfolioError(INVALID_TRANSACTION, "transaction `type` is required")
        try:
            txn_type = TransactionType(str(raw_type).upper())
        except ValueError:
            return PortfolioError(UNSUPPORTED_TRANSACTION, f"unsupported transaction type {raw_type!r}",
                                  {"type": str(raw_type)})
        try:
            quantity = D_or_none(payload.get("quantity")) or ZERO
            price = D_or_none(payload.get("price"))
            fees = D_or_none(payload.get("fees")) or ZERO
            taxes = D_or_none(payload.get("taxes")) or ZERO
            gross = D_or_none(payload.get("gross_amount") if "gross_amount" in payload else payload.get("grossAmount"))
            net = D_or_none(payload.get("net_amount") if "net_amount" in payload else payload.get("netAmount"))
            basis = D_or_none(payload.get("basis_adjustment") if "basis_adjustment" in payload else payload.get("basisAdjustment"))
            fx = D_or_none(payload.get("fx_rate") if "fx_rate" in payload else payload.get("fxRate"))
        except ValueError as exc:
            return PortfolioError(INVALID_TRANSACTION, str(exc))
        symbol = payload.get("symbol")
        symbol = str(symbol).upper() if symbol else None
        if price is None and gross is not None and quantity:
            price = gross / abs(quantity)
        return Transaction(
            portfolio_id=portfolio_id,
            transaction_type=txn_type,
            symbol=symbol,
            quantity=quantity,
            price=price,
            fees=fees,
            taxes=taxes,
            gross_amount=gross,
            net_amount=net,
            basis_adjustment=basis,
            lot_selection=payload.get("lot_selection") or payload.get("lotSelection"),
            currency=(payload.get("currency") or "USD").upper(),
            fx_rate=fx,
            trade_date=payload.get("trade_date") or payload.get("tradeDate"),
            settlement_date=payload.get("settlement_date") or payload.get("settlementDate"),
            timestamp=payload.get("timestamp"),
            account_id=payload.get("account_id") or payload.get("accountId"),
            instrument_id=payload.get("instrument_id") or payload.get("instrumentId"),
            asset_type=payload.get("asset_type") or payload.get("assetType"),
            external_id=payload.get("external_id") or payload.get("externalId"),
            source=payload.get("source") or "manual",
            notes=payload.get("notes"),
            correction_of=payload.get("correction_of") or payload.get("correctionOf"),
            created_by=payload.get("created_by") or payload.get("createdBy"),
        )

    def _validate(self, tx: Transaction, portfolio: dict[str, Any]) -> Optional[PortfolioError]:
        t = tx.transaction_type
        instrument_event = t in INSTRUMENT_EVENTS
        if instrument_event and not tx.symbol:
            return PortfolioError(INVALID_TRANSACTION, f"{t.value} requires an instrument symbol")
        if t in (TransactionType.BUY, TransactionType.SELL):
            if tx.quantity < 0:
                return PortfolioError(NEGATIVE_QUANTITY, "quantity cannot be negative (shorts are not supported)")
            if tx.quantity == 0:
                return PortfolioError(ZERO_QUANTITY, f"{t.value} requires a non-zero quantity")
        if t == TransactionType.ADJUSTMENT and tx.quantity == 0:
            return PortfolioError(ZERO_QUANTITY, "an adjustment requires a non-zero quantity")
        if t in (TransactionType.SPLIT, TransactionType.REVERSE_SPLIT) and tx.quantity <= 0:
            return PortfolioError(INVALID_TRANSACTION, "a split requires a positive ratio")
        if t == TransactionType.COST_BASIS_ADJUSTMENT and tx.basis_adjustment is None:
            return PortfolioError(INVALID_TRANSACTION, "COST_BASIS_ADJUSTMENT requires a basis_adjustment")
        return lot_selection_error(tx, self._method(portfolio))

    async def _mark_async(self, symbol: str, portfolio: dict[str, Any]) -> Optional[Decimal]:
        adapter = MarketAdapter(gateway=self.gateway, base_currency=portfolio.get("baseCurrency") or "USD")
        mark = await adapter.mark(symbol)
        return mark.price


# ── Module helpers ────────────────────────────────────────────────────────────

def _currency_of(txs: Sequence[Transaction], symbol: str) -> Optional[str]:
    for tx in reversed(txs):
        if tx.symbol == symbol and tx.currency:
            return tx.currency
    return None


def _symbols(txs: Sequence[Transaction]) -> list[str]:
    seen: list[str] = []
    for tx in txs:
        if tx.symbol and tx.symbol not in seen:
            seen.append(tx.symbol)
    return seen


def _to_base(native: Optional[Decimal], fx: Optional[Decimal]) -> Optional[Decimal]:
    if native is None:
        return None
    if fx is None or fx == 0:
        return None
    return native / fx


def _normalize_weights(values: dict[str, float]) -> dict[str, float]:
    total = sum(abs(v) for v in values.values())
    if total <= 0:
        return {k: 0.0 for k in values}
    return {k: abs(v) / total for k, v in values.items()}


def _serialize_position(p: dict[str, Any]) -> dict[str, Any]:
    return {
        "symbol": p["symbol"],
        "name": p["name"],
        "assetClass": p["assetClass"],
        "sector": p["sector"],
        "country": p["country"],
        "strategy": p["strategy"],
        "currency": p["currency"],
        "quantity": qty_str(p["quantity"]),
        "averageCost": money_str(p["averageCost"]) if p["averageCost"] is not None else None,
        "costBasis": money_str(p["costBasis"]),
        "costBasisBase": money_str(p["costBasisBase"]),
        "price": money_str(p["price"]) if p["price"] is not None else None,
        "marketValue": money_str(p["marketValue"]) if p["marketValue"] is not None else None,
        "marketValueBase": money_str(p["marketValueBase"]) if p["marketValueBase"] is not None else None,
        "fxPerBase": money_str(p["fxPerBase"]) if p["fxPerBase"] is not None else None,
        "unrealizedPnl": money_str(p["unrealizedPnl"]) if p["unrealizedPnl"] is not None else None,
        "unrealizedPnlBase": money_str(p["unrealizedPnlBase"]) if p["unrealizedPnlBase"] is not None else None,
        "realizedPnl": money_str(p["realizedPnl"]),
        "realizedPnlBase": money_str(p["realizedPnlBase"]),
        "weight": p["weight"],
        "priceFreshness": p["mark"].freshness.value,
        "priceTimestamp": p["mark"].timestamp,
        "priceSource": p["mark"].source,
    }


def _provenance(base: str, positions: Sequence[dict[str, Any]]) -> dict[str, Any]:
    entries = []
    for p in positions:
        mark: Mark = p["mark"]
        entries.append({
            "symbol": p["symbol"],
            "quantity": qty_str(p["quantity"]),
            "price": money_str(p["price"]) if p["price"] is not None else None,
            "priceTimestamp": mark.timestamp,
            "priceSource": mark.source,
            "fxPerBase": money_str(p["fxPerBase"]) if p["fxPerBase"] is not None else None,
            "valuationFormula": f"quantity × price ÷ fx({base})",
        })
    return {
        "calculationVersion": CALCULATION_VERSION,
        "baseCurrency": base,
        "prices": entries,
        "note": "every price names its source and timestamp; FX converts native value to the base currency",
    }


def _nav_from_snapshots(snapshots: Sequence[dict[str, Any]]) -> tuple[list[float], list[float]]:
    ordered = sorted(snapshots, key=lambda s: s.get("asOf") or "")
    nav: list[float] = []
    for s in ordered:
        try:
            nav.append(float(s["totalValue"]))
        except (TypeError, ValueError):
            continue
    return nav, [0.0] * len(nav)


def _failed(error: PortfolioError, warnings: Optional[list[str]] = None) -> dict[str, Any]:
    return {
        "status": "FAILED",
        "error": error.to_dict(),
        "warnings": list(warnings or []),
    }


__all__ = ["PortfolioService", "CALCULATION_VERSION"]
