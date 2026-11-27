"""The broker connection service (P0, read-only).

One service over the adapter registry and the durable connection store. It has
no order path: every method here reads. A missing broker is an envelope with
`available: false` and a named reason, never an empty portfolio that looks real.

Writes are limited to the connection's own status/audit row.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from .adapter import AdapterUnavailable, TradingAdapter
from .authorizer import (
    APPROVAL_KIND,
    approval_view,
    consume_approval,
    cooling_remaining,
    evaluate_guardrails,
    money_intent_for,
    verify_approval,
)
from .mandates import TradingMandateStore
from .model import (
    BrokerConfigError,
    BrokerConnection,
    ConnectionMode,
    OrderIntent,
    TradeRefused,
    normalise_mode,
    ticket_ref,
    validate_config,
)
from .policy import TradePolicy
from .ownership import with_ownership_magic
from .reconcile import BrokerReconciler, ReconcilerLoop, interval_from_env
from .registry import get_kind, list_kinds, make_adapter
from .store import BrokerConnectionStore
from .terminal import reveal_terminal


class BrokerService:
    def __init__(
        self,
        store: Any,
        *,
        approvals: Any = None,
        guardrails: Any = None,
    ) -> None:
        self._store = store
        self.connections = BrokerConnectionStore(store)
        self.mandates = TradingMandateStore(store)
        self.approvals = approvals
        self.guardrails = guardrails
        self._reconciler_loop: Optional[ReconcilerLoop] = None

    # ── kinds ────────────────────────────────────────────────────────────────
    def kinds(self) -> list[dict[str, Any]]:
        return list_kinds()

    # ── connection records ───────────────────────────────────────────────────
    def list_connections(self) -> list[dict[str, Any]]:
        # Through `_connection` so an old connection's missing magic is repaired
        # before any caller can act on it.
        return [self._connection(c.id).to_dict() for c in self.connections.list()]

    def get_connection(self, connection_id: str) -> dict[str, Any]:
        return self._connection(connection_id).to_dict()

    def create_connection(
        self,
        *,
        kind: str,
        label: str,
        mode: Any = ConnectionMode.DEMO,
        config: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        get_kind(kind)  # unknown kind is a caller error, not a stored row
        # Every connection gets an ownership MAGIC: it is what the kill switch
        # matches, so an Eclipse order is cancellable without touching anything
        # else on the account.
        config, _magic = with_ownership_magic(config, label)
        connection = self.connections.create(
            kind=kind, label=label, mode=mode, config=config
        )
        self.connections.record_event(
            connection.id, "create", "ok",
            {"kind": connection.kind, "mode": connection.mode.value},
        )
        return connection.to_dict()

    def update_connection(self, connection_id: str, patch: dict[str, Any]) -> dict[str, Any]:
        current = self.connections.get(connection_id)
        if current is None:
            raise KeyError(f"no broker connection {connection_id!r}")
        updated = self.connections.update(connection_id, patch or {})
        if updated is None:
            raise KeyError(f"no broker connection {connection_id!r}")
        if "mode" in (patch or {}) or "config" in (patch or {}):
            # A changed mode or cap invalidates any live arm.
            self.connections.clear_arm(connection_id)
            self.connections.record_event(connection_id, "disarm", "config-changed")
            updated = self.connections.get(connection_id) or updated
        self.connections.record_event(connection_id, "update", "ok", {"fields": sorted(patch)})
        return updated.to_dict()

    def delete_connection(self, connection_id: str) -> bool:
        deleted = self.connections.delete(connection_id)
        if deleted:
            self.connections.record_event(connection_id, "delete", "ok")
        return deleted

    # ── reads ────────────────────────────────────────────────────────────────
    def _connection(self, connection_id: str) -> BrokerConnection:
        connection = self.connections.get(connection_id)
        if connection is None:
            raise KeyError(f"no broker connection {connection_id!r}")
        # Backfill the magic on connections created before it existed; without
        # it their orders are indistinguishable from a human's and the kill
        # switch refuses to act at all.
        config, magic = with_ownership_magic(connection.config, connection.label)
        if int(float((connection.config or {}).get("magic") or 0) or 0) <= 0:
            updated = self.connections.update(connection_id, {"config": config})
            if updated is not None:
                self.connections.record_event(
                    connection_id, "update", "ok",
                    {"fields": ["magic"], "magic": magic},
                )
                connection = updated
        return connection

    def _unavailable(self, connection: BrokerConnection, failure: AdapterUnavailable) -> dict[str, Any]:
        self.connections.set_status(
            connection.id, status="unavailable", reason=failure.reason
        )
        self.connections.record_event(
            connection.id, "read", "unavailable", failure.to_dict()
        )
        return {
            "available": False,
            "status": "UNAVAILABLE",
            "connectionId": connection.id,
            "kind": connection.kind,
            "mode": connection.mode.value,
            "error": failure.to_dict(),
        }

    def _read(self, connection_id: str, operation: str) -> dict[str, Any]:
        connection = self._connection(connection_id)
        adapter: Any = None
        try:
            adapter = make_adapter(connection.kind, connection.config)
        except KeyError as exc:
            raise AdapterUnavailable("BROKER_KIND_UNKNOWN", str(exc)) from exc
        try:
            if operation == "account":
                payload = adapter.account()
            elif operation == "positions":
                payload = adapter.positions()
            elif operation == "orders":
                payload = adapter.orders()
            else:  # pragma: no cover - guarded by the router
                raise ValueError(f"unknown read operation {operation!r}")
        except AdapterUnavailable as exc:
            return self._unavailable(connection, exc)
        except Exception as exc:  # noqa: BLE001 - an adapter bug is not a broker fact
            failure = AdapterUnavailable(
                "ADAPTER_ERROR", f"{connection.kind} adapter failed: {exc}"
            )
            return self._unavailable(connection, failure)
        finally:
            try:
                if adapter is not None:
                    adapter.close()
            except Exception:  # noqa: BLE001
                pass

        if operation == "account" and isinstance(payload, dict):
            self.connections.set_status(
                connection.id,
                status="connected",
                reason=None,
                account=payload,
                mark_seen=True,
            )
        else:
            self.connections.set_status(
                connection.id, status="connected", reason=None, mark_seen=True
            )
        self.connections.record_event(connection.id, operation, "ok")
        return {
            "available": True,
            "status": "connected",
            "connectionId": connection.id,
            "kind": connection.kind,
            "mode": connection.mode.value,
            "data": payload,
        }

    def account(self, connection_id: str) -> dict[str, Any]:
        return self._read(connection_id, "account")

    def positions(self, connection_id: str) -> dict[str, Any]:
        return self._read(connection_id, "positions")

    def orders(self, connection_id: str) -> dict[str, Any]:
        return self._read(connection_id, "orders")

    def quote(self, connection_id: str, symbol: str) -> dict[str, Any]:
        connection = self._connection(connection_id)
        adapter: Any = None
        try:
            adapter = make_adapter(connection.kind, connection.config)
        except KeyError as exc:
            raise AdapterUnavailable("BROKER_KIND_UNKNOWN", str(exc)) from exc
        try:
            payload = adapter.quote(symbol)
        except AdapterUnavailable as exc:
            return self._unavailable(connection, exc)
        except Exception as exc:  # noqa: BLE001
            failure = AdapterUnavailable(
                "ADAPTER_ERROR", f"{connection.kind} adapter failed: {exc}"
            )
            return self._unavailable(connection, failure)
        finally:
            try:
                if adapter is not None:
                    adapter.close()
            except Exception:  # noqa: BLE001
                pass
        self.connections.set_status(
            connection.id, status="connected", reason=None, mark_seen=True
        )
        self.connections.record_event(
            connection.id, "quote", "ok", {"symbol": payload.get("symbol")}
        )
        return {
            "available": True,
            "status": "connected",
            "connectionId": connection.id,
            "kind": connection.kind,
            "mode": connection.mode.value,
            "data": payload,
        }

    def bars(
        self, connection_id: str, symbol: str, timeframe: str = "H1", count: int = 120,
    ) -> dict[str, Any]:
        """The broker's own OHLCV series, as a read envelope like `quote`."""
        connection = self._connection(connection_id)
        adapter: Any = None
        try:
            adapter = make_adapter(connection.kind, connection.config)
        except KeyError as exc:
            raise AdapterUnavailable("BROKER_KIND_UNKNOWN", str(exc)) from exc
        try:
            payload = adapter.bars(symbol, timeframe=timeframe, count=count)
        except AdapterUnavailable as exc:
            return self._unavailable(connection, exc)
        except Exception as exc:  # noqa: BLE001
            failure = AdapterUnavailable(
                "ADAPTER_ERROR", f"{connection.kind} adapter failed: {exc}"
            )
            return self._unavailable(connection, failure)
        finally:
            try:
                if adapter is not None:
                    adapter.close()
            except Exception:  # noqa: BLE001
                pass
        self.connections.set_status(
            connection.id, status="connected", reason=None, mark_seen=True
        )
        self.connections.record_event(
            connection.id, "bars", "ok",
            {"symbol": payload.get("symbol"), "timeframe": payload.get("timeframe")},
        )
        return {
            "available": True, "status": "connected",
            "connectionId": connection.id, "kind": connection.kind,
            "mode": connection.mode.value, "data": payload,
        }

    # ── demo automation mandates (P3) ────────────────────────────────────────
    def create_mandate(self, connection_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        connection = self._connection(connection_id)
        mode = connection.mode.value
        expected = f"MANDATE {mode.upper()} {connection.label}"
        if str(payload.get("confirmation") or "").strip() != expected:
            raise TradeRefused(
                "MANDATE_CONFIRMATION_MISMATCH",
                f"type {expected!r} exactly to create this mandate",
                status_code=400,
            )
        if mode == "live":
            # The second key: an unattended live mandate can only be created on an
            # armed live connection. Execution re-checks the arm again. The pin
            # (workflow, or bot + objective version) is enforced by the mandate
            # store itself (`MANDATE_WORKFLOW_REQUIRED`), which is the ONE place a
            # mandate is created — this method must not re-state it with a weaker
            # rule.
            self._assert_live_armed(connection)
        bot_id = str(payload.get("botId") or "").strip()
        bot_version = (
            int(payload["botVersion"]) if payload.get("botVersion") is not None else None
        )
        if bot_id:
            # A bot-pinned live grant must match the bot's OWN leash: tier L2, the
            # current objective version, and no margin above its ceiling.
            from .authority import check_mandate_creation  # noqa: PLC0415

            check_mandate_creation(
                self, bot_id=bot_id, bot_version=bot_version,
                max_margin=float(payload.get("maxMargin") or 0),
                max_orders=int(payload.get("maxOrders") or 0), mode=mode,
            )
        mandate = self.mandates.create(
            connection=connection,
            symbols=list(payload.get("symbols") or ["*"]),
            max_orders=int(payload.get("maxOrders") or 20),
            max_margin=float(payload.get("maxMargin") or 0),
            ttl_seconds=int(payload.get("ttlSeconds") or 0),
            note=str(payload.get("note") or ""),
            workflow_id=str(payload.get("workflowId") or ""),
            workflow_version=(
                int(payload.get("workflowVersion"))
                if payload.get("workflowVersion") is not None else None
            ),
            bot_id=bot_id,
            bot_version=bot_version,
        )
        self.connections.record_event(
            connection.id, "mandate", "created",
            {"mandateId": mandate["id"], "expiresAt": mandate["expiresAt"]},
        )
        return mandate

    def list_mandates(
        self, connection_id: Optional[str] = None, limit: int = 50, *, active_only: bool = False,
    ) -> list[dict[str, Any]]:
        if active_only:
            return self.mandates.list_active(connection_id, limit=limit)
        return self.mandates.list(connection_id, limit=limit)

    def revoke_mandate(self, mandate_id: str) -> dict[str, Any]:
        mandate = self.mandates.revoke(mandate_id)
        if mandate is None:
            raise TradeRefused("MANDATE_NOT_FOUND", "that mandate does not exist", status_code=404)
        self.connections.record_event(
            mandate["connectionId"], "mandate", "revoked", {"mandateId": mandate_id}
        )
        return mandate

    def revoke_mandates_for_bot(self, bot_id: str) -> list[dict[str, Any]]:
        """Revoke every active mandate pinned to this bot.

        Called when a bot is paused/disabled/archived/deleted. Positions are never
        closed here: closing is the operator's risk-reducing decision.
        """
        if not str(bot_id or "").strip():
            return []
        revoked: list[dict[str, Any]] = []
        for mandate in self.mandates.list_active(None, limit=500):
            if str(mandate.get("botId") or "") != str(bot_id):
                continue
            try:
                revoked.append(self.revoke_mandate(mandate["id"]))
            except TradeRefused:
                continue
        return revoked

    # ── execution (P1 demo + P2 armed live) ──────────────────────────────────
    def _execution_connection(self, connection_id: str) -> tuple[BrokerConnection, str]:
        connection = self._connection(connection_id)
        if connection.mode == ConnectionMode.DEMO:
            return connection, "demo"
        if connection.mode == ConnectionMode.LIVE:
            self._assert_live_armed(connection)
            return connection, "live"
        raise TradeRefused(
            "EXECUTION_DISABLED",
            "this connection has no execution mode; set it to demo or live",
            status_code=403,
        )

    def _trading_adapter(self, connection: BrokerConnection) -> TradingAdapter:
        try:
            adapter = make_adapter(connection.kind, connection.config)
        except KeyError as exc:
            raise TradeRefused("BROKER_KIND_UNKNOWN", str(exc)) from exc
        if not isinstance(adapter, TradingAdapter):
            raise TradeRefused(
                "BROKER_READ_ONLY",
                f"the {connection.kind} adapter has no execution methods",
            )
        return adapter

    def _assert_daily_loss(
        self, connection: BrokerConnection, adapter: Any, preview: dict[str, Any],
    ) -> None:
        """Rolling 24 h loss brake: block new exposure, never block an exit.

        Realised P&L comes from the broker's own deals; open P&L comes from the
        account snapshot. If the history read fails the guard still uses the open
        P&L, so a dead history route can never disable the brake entirely.
        """
        policy = TradePolicy.from_connection(connection)
        if policy.max_daily_loss_pct <= 0:
            return
        account = preview.get("account") or {}
        equity = _to_float(account.get("equity"))
        if equity <= 0:
            return
        open_pnl = _to_float(account.get("profit"))
        realised = 0.0
        if hasattr(adapter, "deals_since"):
            since_ms = int(
                (datetime.now(timezone.utc) - timedelta(hours=24)).timestamp() * 1000
            )
            try:
                realised = sum(
                    _to_float(deal.get("profit"))
                    for deal in (adapter.deals_since(since_ms) or [])
                )
            except Exception:  # noqa: BLE001 - open P&L still applies
                realised = 0.0
        total = open_pnl + realised
        cap = equity * policy.max_daily_loss_pct / 100.0
        if total <= -cap:
            raise TradeRefused(
                "DAILY_LOSS_LIMIT",
                f"rolling 24h P&L is {total:,.2f}, at or below the "
                f"{policy.max_daily_loss_pct:g}% brake ({cap:,.2f} on "
                f"{equity:,.2f} equity). New entries are blocked; closing "
                "positions is still available.",
                status_code=403,
            )

    @staticmethod
    def _assert_account_mode(preview: dict[str, Any], expected: str) -> None:
        account = preview.get("account") or {}
        mode = str(account.get("mode") or "unknown")
        want = "demo" if expected == "demo" else "real"
        if mode != want:
            raise TradeRefused(
                "DEMO_ACCOUNT_REQUIRED" if expected == "demo" else "LIVE_ACCOUNT_REFUSED",
                f"the broker account reports mode {mode!r}; this connection is {expected}",
                status_code=403,
            )
        if not bool(account.get("tradeAllowed")):
            raise TradeRefused(
                "TRADING_DISABLED",
                "the broker account has trading disabled",
                status_code=403,
            )
        if preview.get("terminalTradeAllowed") is False:
            raise TradeRefused(
                "ALGO_TRADING_DISABLED",
                "enable Algo Trading in the terminal before submitting an order",
                status_code=403,
            )
        if expected == "live" and not bool(account.get("expertAllowed")):
            raise TradeRefused(
                "ALGO_TRADING_DISABLED",
                "enable Algo Trading in the terminal before submitting a live order",
                status_code=403,
            )

    def _assert_live_armed(self, connection: BrokerConnection) -> None:
        raw = str(connection.armed_until or "")
        if not raw:
            raise TradeRefused(
                "LIVE_NOT_ARMED",
                "live execution is not armed; arm the connection in Settings › Trading",
                status_code=403,
            )
        try:
            until = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            self.connections.clear_arm(connection.id)
            raise TradeRefused(
                "LIVE_NOT_ARMED",
                "the live arm is invalid; arm the connection again",
                status_code=403,
            ) from None
        if datetime.now(timezone.utc) >= until:
            self.connections.clear_arm(connection.id)
            raise TradeRefused(
                "LIVE_ARM_EXPIRED",
                "the live arm expired; arm the connection again",
                status_code=403,
            )

    def _live_cap(self, connection: BrokerConnection) -> float:
        try:
            return float(connection.config.get("maxMargin") or 0)
        except (TypeError, ValueError):
            return 0.0

    def arm_connection(
        self,
        connection_id: str,
        *,
        confirmation: str = "",
        ttl_seconds: int = 1800,
    ) -> dict[str, Any]:
        connection = self._connection(connection_id)
        if connection.mode != ConnectionMode.LIVE:
            raise TradeRefused(
                "ARM_REQUIRES_LIVE",
                "only a connection set to live can be armed",
                status_code=400,
            )
        if self._magic(connection) <= 0:
            raise TradeRefused(
                "LIVE_MAGIC_REQUIRED",
                "set a non-zero magic on the connection before arming live; it scopes "
                "the kill switch to Eclipse's own orders",
                status_code=400,
            )
        if self._live_cap(connection) <= 0:
            raise TradeRefused(
                "LIVE_CAP_REQUIRED",
                "set a positive maxMargin on the connection before arming live",
                status_code=400,
            )
        adapter = self._trading_adapter(connection)
        try:
            probe = adapter.probe()
        except AdapterUnavailable as exc:
            raise TradeRefused(exc.code, exc.reason, status_code=503) from exc
        finally:
            try:
                adapter.close()
            except Exception:  # noqa: BLE001
                pass
        account = (probe or {}).get("account") or {}
        terminal = (probe or {}).get("terminal") or {}
        if not (probe or {}).get("available") or not account:
            raise TradeRefused(
                "LIVE_ACCOUNT_UNAVAILABLE",
                "connect the terminal to a live account before arming",
                status_code=503,
            )
        if not bool(terminal.get("tradeAllowed") or account.get("tradeAllowed")):
            mt5 = connection.kind == "mt5"
            raise TradeRefused(
                "ALGO_TRADING_DISABLED" if mt5 else "LIVE_TRADING_DISABLED",
                (
                    "enable Algo Trading in the terminal before arming live"
                    if mt5 else
                    "the broker reports trading disabled for this account; enable it "
                    "at the broker before arming live"
                ),
                status_code=403,
            )
        self._assert_account_mode({"account": account}, "live")
        expected = f"ARM LIVE {account.get('armRef') or account.get('login')}"
        if str(confirmation or "").strip() != expected:
            raise TradeRefused(
                "LIVE_CONFIRMATION_MISMATCH",
                f"type {expected!r} exactly to arm this account",
                status_code=400,
            )
        ttl = max(60, min(int(ttl_seconds or 1800), 28_800))
        until = (datetime.now(timezone.utc) + timedelta(seconds=ttl)).isoformat().replace(
            "+00:00", "Z"
        )
        self.connections.set_arm(connection.id, until=until, by="operator")
        self.connections.record_event(
            connection.id, "arm", "ok", {"until": until, "ttlSeconds": ttl}
        )
        return {
            "available": True,
            "armed": True,
            "connectionId": connection.id,
            "armedUntil": until,
            "ttlSeconds": ttl,
            "account": account,
        }

    def disarm_connection(self, connection_id: str) -> dict[str, Any]:
        connection = self._connection(connection_id)
        self.connections.clear_arm(connection.id)
        self.connections.record_event(connection.id, "disarm", "ok")
        return {"available": True, "armed": False, "connectionId": connection.id}

    def kill_switch(self, connection_id: str) -> dict[str, Any]:
        """Cancel every working order Eclipse owns, then disarm."""
        connection = self._connection(connection_id)
        magic = self._magic(connection)
        if magic <= 0:
            raise TradeRefused(
                "MAGIC_REQUIRED",
                "set a non-zero magic on the connection before using the kill switch",
                status_code=400,
            )
        adapter = self._trading_adapter(connection)
        try:
            try:
                result = adapter.cancel_all(magic)
            except AdapterUnavailable as exc:
                raise TradeRefused(exc.code, exc.reason, status_code=503) from exc
        finally:
            try:
                adapter.close()
            except Exception:  # noqa: BLE001
                pass
        self.connections.clear_arm(connection.id)
        self.connections.record_event(
            connection.id, "kill", "ok" if result.get("ok") else "partial", result
        )
        return {
            "available": True,
            "connectionId": connection.id,
            "armed": False,
            "magic": magic,
            "kill": result,
        }

    @staticmethod
    def _magic(connection: BrokerConnection) -> int:
        try:
            return int(connection.config.get("magic") or 0)
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _public_approval(approval: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
        if not approval:
            return None
        return {
            "id": approval.get("id"),
            "kind": approval.get("kind"),
            "status": approval.get("status"),
            "actionSummary": approval.get("actionSummary"),
            "risk": approval.get("risk"),
            "expiresAt": approval.get("expiresAt"),
            "createdAt": approval.get("createdAt"),
        }

    def preview_order(
        self, connection_id: str, payload: dict[str, Any], *,
        record_approval: bool = True,
        run_id: str = "",
        automation_id: str = "",
    ) -> dict[str, Any]:
        """Validate, price-check and request a single-use approval. Sends nothing.

        `record_approval=False` runs the identical validation without creating an
        approval row — the dry-run path a graph uses, so previewing a workflow
        never leaves approval cards behind. `run_id` binds the row to the run that
        requested it, which is how an unattended automation reports its own
        pending orders. The body lives in `trade/preview.py` so this service stays
        below the module size ceiling.
        """
        from .preview import preview_order  # noqa: PLC0415 - avoids an import cycle

        return preview_order(
            self, connection_id, payload,
            record_approval=record_approval, run_id=run_id,
            automation_id=automation_id,
        )

    def submit_order(
        self,
        connection_id: str,
        payload: dict[str, Any],
        approval_id: str = "",
        confirmation: str = "",
        mandate_id: str = "",
        run_id: str = "",
        workflow_id: str = "",
        node_id: str = "",
        automation_id: str = "",
        automation_version: int | None = None,
        bot_id: str = "",
        bot_version: int | None = None,
    ) -> dict[str, Any]:
        """Consume the authorization and send exactly one order. Never retries."""
        if approval_id and mandate_id:
            raise TradeRefused(
                "AMBIGUOUS_AUTHORIZATION",
                "provide either an approvalId or a mandateId, not both",
                status_code=400,
            )
        if not approval_id and not mandate_id:
            raise TradeRefused(
                "AUTHORIZATION_REQUIRED",
                "an approvalId (manual) or mandateId (unattended demo) is required",
                status_code=400,
            )
        connection, execution_mode = self._execution_connection(connection_id)
        intent = OrderIntent.from_payload(
            payload, connection_id=connection.id, magic_default=self._magic(connection)
        )
        # One fence for a bot reaching a LIVE account: every caller passes
        # `automation_id`, so no path can forget it (graphs pass none).
        if automation_id and execution_mode == "live":
            from .authority import check_live_order  # noqa: PLC0415

            refusal = check_live_order(
                self, automation_id=automation_id, connection_id=connection.id,
                mode=execution_mode, intent=intent.to_dict(),
            )
            if refusal is not None:
                raise TradeRefused(
                    str(refusal[1].get("reason") or "BOT_REFUSED"), refusal[0],
                    status_code=409,
                )
        approval_row: dict[str, Any] = {}
        if approval_id:
            verify_approval(self.approvals, approval_id, intent)
            approval_row = self.approvals.get(approval_id) or {}
        adapter = self._trading_adapter(connection)
        try:
            try:
                preview = adapter.order_preview(intent)
            except AdapterUnavailable as exc:
                raise TradeRefused(exc.code, exc.reason, status_code=503) from exc
            self._assert_account_mode(preview, execution_mode)
            policy = TradePolicy.from_connection(connection).evaluate(preview)
            if not preview.get("ok") or not policy.get("allowed"):
                reasons = list(preview.get("reasons") or []) + list(policy.get("reasons") or [])
                self.connections.record_event(
                    connection.id, "submit", "refused",
                    {"digest": intent.digest, "stage": "preview"},
                )
                return {
                    "available": True,
                    "sent": False,
                    "filled": False,
                    "status": "REFUSED",
                    "stage": "preview",
                    "connectionId": connection.id,
                    "preview": preview,
                    "policy": policy,
                    "reasons": reasons,
                }

            mandate: Optional[dict[str, Any]] = None
            self._assert_daily_loss(connection, adapter, preview)
            if mandate_id:
                mandate = self.mandates.verify(
                    mandate_id,
                    connection_id=connection.id,
                    symbol=str(preview.get("symbol") or intent.symbol),
                    margin=float(preview.get("margin") or 0),
                    mode=execution_mode,
                    workflow_id=workflow_id,
                    bot_id=bot_id,
                )
                if bot_id and bot_version is not None:
                    pinned = mandate.get("botVersion")
                    if pinned is not None and int(pinned) != int(bot_version):
                        raise TradeRefused(
                            "BOT_VERSION_STALE",
                            "this bot's objective changed since the mandate was "
                            "granted; re-grant its live authority",
                            status_code=409,
                        )

            money_intent = money_intent_for(connection, intent, preview)
            verdict = evaluate_guardrails(self.guardrails, money_intent, record=False)
            if str(verdict.decision) in ("deny", "escalate"):
                self.connections.record_event(
                    connection.id, "submit", "refused",
                    {"digest": intent.digest, "stage": "guardrails"},
                )
                return {
                    "available": True,
                    "sent": False,
                    "filled": False,
                    "status": "REFUSED",
                    "stage": "guardrails",
                    "connectionId": connection.id,
                    "preview": preview,
                    "guardrail": verdict.to_dict(),
                    "reasons": list(getattr(verdict, "reasons", []) or []),
                }

            if execution_mode == "live":
                expected = (
                    f"LIVE {intent.side.upper()} "
                    f"{float(preview.get('volume') or intent.volume):g} "
                    f"{preview.get('symbol') or intent.symbol}"
                )
                # A mandate is the standing authorization for an unattended live
                # order; a manual live order still needs the typed phrase.
                required = not bool(mandate_id)
                phrase_reason = "live orders require the typed order phrase"
            else:
                expected = str(verdict.confirm_phrase or "")
                required = bool(verdict.requires_typed_confirmation)
                phrase_reason = "the money guardrails require the typed confirmation phrase"
            if required and str(confirmation or "").strip() != expected:
                self.connections.record_event(
                    connection.id, "submit", "confirmation-required",
                    {"digest": intent.digest},
                )
                return {
                    "available": True,
                    "sent": False,
                    "filled": False,
                    "status": "CONFIRMATION_REQUIRED",
                    "connectionId": connection.id,
                    "preview": preview,
                    "requiredPhrase": expected,
                    "reasons": [f"{phrase_reason} before this order can be sent"],
                }

            remaining = cooling_remaining(approval_row, verdict)
            if remaining > 0:
                self.connections.record_event(
                    connection.id, "submit", "cooling",
                    {"digest": intent.digest, "remainingSeconds": remaining},
                )
                return {
                    "available": True,
                    "sent": False,
                    "filled": False,
                    "status": "COOLING_OFF",
                    "connectionId": connection.id,
                    "preview": preview,
                    "remainingSeconds": remaining,
                    "reasons": [
                        f"the money guardrails require {remaining} more second(s) of "
                        "cooling-off before this order can be sent"
                    ],
                }

            # Record the intent only once the order is about to be authorized.
            verdict = evaluate_guardrails(self.guardrails, money_intent, record=True)
            if str(verdict.decision) in ("deny", "escalate"):
                self.connections.record_event(
                    connection.id, "submit", "refused",
                    {"digest": intent.digest, "stage": "guardrails-final"},
                )
                return {
                    "available": True,
                    "sent": False,
                    "filled": False,
                    "status": "REFUSED",
                    "stage": "guardrails",
                    "connectionId": connection.id,
                    "preview": preview,
                    "guardrail": verdict.to_dict(),
                    "reasons": list(getattr(verdict, "reasons", []) or []),
                }

            if approval_id:
                if execution_mode == "live":
                    self._assert_live_armed(connection)
                consume_approval(self.approvals, approval_id, intent)
            else:
                self.mandates.consume(mandate_id)
            ledger = self.connections.create_order(
                connection_id=connection.id,
                intent=intent.to_dict(),
                digest=intent.digest,
                approval_id=approval_id,
                mandate_id=mandate_id,
                run_id=run_id,
                node_id=node_id,
                automation_id=automation_id,
                automation_version=automation_version,
            )
            self.connections.record_event(
                connection.id, "submit", "sent",
                {
                    "digest": intent.digest,
                    "orderId": ledger["id"],
                    "mandateId": mandate_id or None,
                    "runId": run_id or None,
                    "nodeId": node_id or None,
                    "automationId": automation_id or None,
                },
            )
            try:
                result = adapter.submit_order(intent)
            except AdapterUnavailable as exc:
                self.connections.update_order(
                    ledger["id"], status="unknown", error=exc.reason
                )
                return {
                    "available": True,
                    "sent": None,
                    "filled": False,
                    "status": "UNKNOWN",
                    "connectionId": connection.id,
                    "orderId": ledger["id"],
                    "error": exc.to_dict(),
                    "reasons": [
                        "the broker did not return a definitive result; check the "
                        "terminal before retrying"
                    ],
                }
            except Exception as exc:  # noqa: BLE001
                self.connections.update_order(
                    ledger["id"], status="unknown", error=str(exc)
                )
                return {
                    "available": True,
                    "sent": None,
                    "filled": False,
                    "status": "UNKNOWN",
                    "connectionId": connection.id,
                    "orderId": ledger["id"],
                    "error": {"code": "ADAPTER_ERROR", "message": str(exc)},
                    "reasons": [
                        "the adapter failed after the order may have been sent; check "
                        "the terminal before retrying"
                    ],
                }

            status = _order_status(result)
            code = result.get("retcode")
            self.connections.update_order(
                ledger["id"],
                status=status,
                ticket=result.get("ticket"),
                retcode=code,
                retcodeName=result.get("retcodeName"),
                filledVolume=result.get("volume"),
                avgPrice=result.get("price"),
                error=None if status in ("filled", "placed") else (
                    "; ".join(result.get("reasons") or []) or None
                ),
            )
            self.connections.record_event(
                connection.id, "submit", status,
                {"digest": intent.digest, "orderId": ledger["id"], "retcode": code},
            )
            if status in ("filled", "placed"):
                # Local affordance: show the operator the new position in the
                # attached terminal. Never allowed to change the order result.
                try:
                    reveal_terminal(connection)
                except Exception:  # noqa: BLE001
                    pass
            return {
                "available": True,
                "sent": True,
                "filled": status == "filled",
                "status": status,
                "connectionId": connection.id,
                "orderId": ledger["id"],
                "ledger": self.connections.get_order(ledger["id"]),
                "result": result,
                "reasons": list(result.get("reasons") or []),
            }
        finally:
            try:
                adapter.close()
            except Exception:  # noqa: BLE001
                pass

    def cancel_order(self, connection_id: str, ticket: Any) -> dict[str, Any]:
        """Cancel a working order. Risk-reducing, so no trade approval is required."""
        connection = self._connection(connection_id)
        ref = ticket_ref(ticket)
        adapter = self._trading_adapter(connection)
        try:
            try:
                result = adapter.cancel_order(ref)
            except AdapterUnavailable as exc:
                raise TradeRefused(exc.code, exc.reason, status_code=503) from exc
        finally:
            try:
                adapter.close()
            except Exception:  # noqa: BLE001
                pass
        self.connections.record_event(
            connection.id,
            "cancel",
            "ok" if result.get("ok") else "rejected",
            {"ticket": ref, "retcode": result.get("retcode")},
        )
        return {
            "available": True,
            "cancelled": bool(result.get("ok")),
            "connectionId": connection.id,
            "result": result,
        }

    def close_position(
        self, connection_id: str, ticket: Any, *, deviation: int = 20,
        volume: Optional[float] = None,
    ) -> dict[str, Any]:
        """Close one demo position (full or partial). Risk-reducing: no approval."""
        from .positions import close_position  # noqa: PLC0415

        return close_position(
            self, connection_id, ticket, deviation=deviation, volume=volume
        )

    def close_positions(
        self, connection_id: str, *, tickets: Optional[list[Any]] = None,
        deviation: int = 20,
    ) -> dict[str, Any]:
        """Close every open demo position, or only the named tickets."""
        from .positions import close_positions  # noqa: PLC0415

        return close_positions(
            self, connection_id, tickets=tickets, deviation=deviation
        )

    def modify_position(
        self, connection_id: str, ticket: Any, *,
        stop_loss: Optional[float] = None, take_profit: Optional[float] = None,
    ) -> dict[str, Any]:
        """Move a demo position's stop/target. Risk-reducing: no approval."""
        from .positions import modify_position  # noqa: PLC0415

        return modify_position(
            self, connection_id, ticket,
            stop_loss=stop_loss, take_profit=take_profit,
        )

    def modify_order(
        self, connection_id: str, ticket: Any, *,
        price: Optional[float] = None,
        stop_loss: Optional[float] = None, take_profit: Optional[float] = None,
    ) -> dict[str, Any]:
        """Re-price/re-bracket a working order. Risk-reducing: no approval."""
        from .positions import modify_order  # noqa: PLC0415

        return modify_order(
            self, connection_id, ticket, price=price,
            stop_loss=stop_loss, take_profit=take_profit,
        )

    def symbol_info(self, connection_id: str, symbol: str) -> dict[str, Any]:
        """Static symbol facts from the broker (no tick required)."""
        from .symbols import symbol_info  # noqa: PLC0415 - avoids an import cycle

        return symbol_info(self, connection_id, symbol)

    def trade_plan(
        self, connection_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        """Size a trade from equity, the stop and the broker's symbol limits.

        Read-only: no order, no approval. The body lives in `trade/plan.py` so
        this service stays below the module size ceiling.
        """
        from .plan import build_trade_plan  # noqa: PLC0415 - avoids an import cycle

        return build_trade_plan(self, connection_id, payload)

    def ledger_orders(
        self, connection_id: Optional[str] = None, limit: int = 50,
        automation_id: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        return self.connections.list_orders(
            connection_id, limit=limit, automation_id=automation_id
        )

    def get_ledger_order(self, order_id: str) -> dict[str, Any]:
        order = self.connections.get_order(order_id)
        if order is None:
            raise KeyError(f"no broker order {order_id!r}")
        return order

    # ── trade approvals ──────────────────────────────────────────────────────
    def list_approvals(self, status: Optional[str] = None, limit: int = 50) -> list[dict[str, Any]]:
        if self.approvals is None:
            return []
        rows = self.approvals.list_kind(APPROVAL_KIND, status, limit=limit)
        return [approval_view(row) for row in rows]

    def decide_approval(self, approval_id: str, *, approve: bool, reason: str = "") -> dict[str, Any]:
        if self.approvals is None:
            raise TradeRefused("APPROVALS_UNAVAILABLE", "the approval store is unavailable", status_code=503)
        current = self.approvals.get(approval_id)
        if not current or current.get("kind") != APPROVAL_KIND:
            raise TradeRefused("APPROVAL_NOT_FOUND", "that trade approval does not exist", status_code=404)
        decided = self.approvals.decide(
            approval_id, approve=approve, decided_by="operator", reason=reason
        )
        self.connections.record_event(
            current.get("serverId", "").removeprefix("trade:"),
            "approval",
            "approved" if approve else "denied",
            {"approvalId": approval_id},
        )
        return decided or {}

    # ── reconciliation ───────────────────────────────────────────────────────
    def sync_connection(self, connection_id: str, history_days: Optional[int] = None) -> dict[str, Any]:
        self._connection(connection_id)
        return BrokerReconciler(self).sync(connection_id, history_days=history_days)

    def smoke_connection(self, connection_id: str, days: int = 7) -> dict[str, Any]:
        """Probe → reads → one sync, with an honest per-check report."""
        self._connection(connection_id)
        from .smoke import run_smoke  # noqa: PLC0415 - keeps the adapter import graph small

        return run_smoke(self, connection_id, days=days)

    def list_fills(
        self,
        connection_id: str,
        limit: int = 100,
        before_dealt_at: Optional[int] = None,
        before_id: Optional[int] = None,
    ) -> list[dict[str, Any]]:
        self._connection(connection_id)
        return self.connections.list_fills(
            connection_id,
            limit=limit,
            before_dealt_at=before_dealt_at,
            before_id=before_id,
        )

    def reconciliation(self, connection_id: str, limit: int = 100) -> dict[str, Any]:
        self._connection(connection_id)
        return {
            "available": True,
            "connectionId": connection_id,
            "state": self.connections.get_sync_state(connection_id),
            "positions": self.connections.list_positions(connection_id),
            "fills": self.connections.list_fills(connection_id, limit=limit),
        }

    async def start_reconciler(self, interval: Optional[int] = None) -> None:
        seconds = interval_from_env() if interval is None else int(interval)
        if seconds <= 0 or self._reconciler_loop is not None:
            return
        self._reconciler_loop = ReconcilerLoop(BrokerReconciler(self), seconds)
        self._reconciler_loop.start()

    async def stop_reconciler(self) -> None:
        loop = self._reconciler_loop
        self._reconciler_loop = None
        if loop is not None:
            await loop.stop()

    # ── probe ────────────────────────────────────────────────────────────────
    def test_connection(self, connection_id: str) -> dict[str, Any]:
        connection = self._connection(connection_id)
        adapter: Any = None
        try:
            adapter = make_adapter(connection.kind, connection.config)
            probe = adapter.probe()
        except KeyError as exc:
            failure = AdapterUnavailable("BROKER_KIND_UNKNOWN", str(exc))
            return self._unavailable(connection, failure)
        except AdapterUnavailable as exc:
            return self._unavailable(connection, exc)
        except Exception as exc:  # noqa: BLE001
            failure = AdapterUnavailable(
                "ADAPTER_ERROR", f"{connection.kind} adapter failed: {exc}"
            )
            return self._unavailable(connection, failure)
        finally:
            try:
                if adapter is not None:
                    adapter.close()
            except Exception:  # noqa: BLE001
                pass

        available = bool(probe.get("available"))
        account = probe.get("account")
        if available and isinstance(account, dict):
            self.connections.set_status(
                connection.id,
                status="connected",
                reason=None,
                account=account,
                mark_seen=True,
            )
        else:
            self.connections.set_status(
                connection.id,
                status="unavailable",
                reason=str(probe.get("reason") or "the broker did not answer"),
            )
            if connection.mode == ConnectionMode.LIVE:
                # A disconnected live terminal must not stay armed.
                self.connections.clear_arm(connection.id)
        self.connections.record_event(
            connection.id,
            "test",
            "ok" if available else "unavailable",
            {
                "code": probe.get("code"),
                "warnings": probe.get("warnings") or [],
            },
        )
        return {
            "available": available,
            "status": "connected" if available else "UNAVAILABLE",
            "connectionId": connection.id,
            "kind": connection.kind,
            "mode": connection.mode.value,
            "probe": probe,
        }

    # ── helpers ──────────────────────────────────────────────────────────────
    def events(self, connection_id: str, limit: int = 20) -> list[dict[str, Any]]:
        return self.connections.list_events(connection_id, limit=limit)

    @staticmethod
    def validate_mode(value: Any) -> str:
        return normalise_mode(value).value

    @staticmethod
    def validate_config(config: Optional[dict[str, Any]]) -> dict[str, Any]:
        return validate_config(config)


_UNKNOWN_RETCODES = {10012, 10031}


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _order_status(result: dict[str, Any]) -> str:
    code = result.get("retcode")
    state = str(result.get("status") or "").strip().lower()
    # MetaTrader's DONE/PARTIAL codes are fills; a REST venue reports its own
    # state, and "accepted" is NOT a fill. The ledger must not call an order
    # filled before the venue has executed it.
    if code == 10008 or state in (
        "placed", "accepted", "new", "working", "open", "pending_new",
        "partially_filled", "partially-filled",
    ):
        return "placed"
    if result.get("ok") or state == "filled":
        return "filled"
    if code in _UNKNOWN_RETCODES or state == "unknown":
        return "unknown"
    return "rejected"


__all__ = ["BrokerService", "BrokerConfigError"]
