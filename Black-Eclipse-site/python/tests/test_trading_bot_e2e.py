"""The full trading-bot chain, in one process.

This is the layer that was missing: it does not fake the runner, the strategy
engine, the trial/mandate or the broker service. It wires the REAL
`run_strategy` -> `LiveStrategyService.run_once` -> `BrokerService.submit_order`
chain and asserts an order actually reaches the adapter, once, with attribution.

The adapter is synthetic (no MT5 terminal), which is correct for CI: the real
wire behaviour is covered by the opt-in demo/live smoke checks.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from harness.automation import trading_runner
from harness.automation.trading_runner import run_strategy
from harness.finance_guardrails.service import FinanceGuardrailService
from harness.mcp.approvals import ApprovalStore
from harness.persistence import AgentMemoryStore
from harness.quant import data as D
from harness.quant import engine as E
from harness.quant import fixtures as F
from harness.quant import strategy as S
from harness.quant.live import LiveStrategyService
from harness.quant.store import QuantStore
from harness.trade.adapter import TradingAdapter
from harness.trade.model import BrokerKind
from harness.trade.registry import register_kind
from harness.trade.service import BrokerService


class FakeAdapter(TradingAdapter):
    kind = "e2e-trade"
    ACCOUNT_MODE = "demo"
    SUBMIT_CALLS = 0
    POSITIONS: list[dict] = []

    @classmethod
    def availability(cls):
        return True, None

    def probe(self):
        return {"available": True, "connected": True, "account": self._account(),
                "terminal": {"connected": True, "tradeAllowed": True}, "warnings": []}

    def _account(self):
        return {
            "login": 7, "mode": self.ACCOUNT_MODE, "server": "Fake", "company": "Fake",
            "currency": "USD", "balance": 100_000.0, "equity": 100_000.0,
            "margin": 0.0, "marginFree": 100_000.0, "marginLevel": 0.0, "profit": 0.0,
            "leverage": 100, "marginMode": "hedging",
            "tradeAllowed": True, "expertAllowed": True,
        }

    def account(self):
        return self._account()

    def positions(self):
        return list(type(self).POSITIONS)

    def orders(self):
        return []

    def quote(self, symbol):
        return {"symbol": symbol.upper(), "bid": 1.1, "ask": 1.1002,
                "spreadPoints": 2.0, "provider": "fake"}

    def order_preview(self, intent):
        return {
            "ok": True, "symbol": intent.symbol.upper(), "side": intent.side,
            "type": intent.order_type, "volume": intent.volume, "price": 1.1002,
            "notional": 1100.2, "margin": 11.0, "marginFree": 100_000.0,
            "spreadPoints": 2.0, "openPositions": len(type(self).POSITIONS),
            "account": self._account(), "terminalTradeAllowed": True,
            "quote": self.quote(intent.symbol), "retcode": 0,
            "retcodeName": "DONE", "reasons": [],
        }

    def submit_order(self, intent):
        type(self).SUBMIT_CALLS += 1
        position = {
            "ticket": 4242, "symbol": intent.symbol.upper(), "side": intent.side,
            "volume": intent.volume, "price": 1.1002, "profit": 0.0,
            "magic": 99, "comment": "e2e",
        }
        type(self).POSITIONS.append(position)
        return {
            "ok": True, "status": "FILLED", "symbol": intent.symbol.upper(),
            "side": intent.side, "type": intent.order_type, "volume": intent.volume,
            "price": 1.1002, "ticket": 4242, "deal": 99, "retcode": 10009,
            "retcodeName": "DONE", "comment": "e2e", "requestId": 1,
            "reasons": [], "account": self._account(),
        }

    def close_position(self, ticket, deviation=20, volume=None):
        type(self).POSITIONS = [p for p in type(self).POSITIONS if p["ticket"] != ticket]
        return {"ok": True, "ticket": ticket, "retcode": 10009,
                "retcodeName": "DONE", "verified": True, "reasons": [],
                "account": self._account()}

    def modify_position(self, ticket, stop_loss=None, take_profit=None):
        return {"ok": True, "ticket": ticket, "stopLoss": stop_loss or 0,
                "takeProfit": take_profit or 0, "retcode": 10009,
                "retcodeName": "DONE", "verified": True, "reasons": [],
                "account": self._account()}

    def cancel_order(self, ticket):
        return {"ok": True, "ticket": ticket, "retcode": 10009,
                "retcodeName": "DONE", "reasons": []}

    def cancel_all(self, magic):
        return {"ok": True, "magic": magic, "matched": 0, "cancelled": 0,
                "failed": 0, "results": []}

    def deals_since(self, since_ms):
        return []


class RealAccountAdapter(FakeAdapter):
    ACCOUNT_MODE = "real"


def _broker(tmp_path, *, live=False):
    store = AgentMemoryStore(os.path.join(str(tmp_path), "e2e.db"))
    approvals = ApprovalStore(store)
    guardrails = FinanceGuardrailService(store, approvals=approvals)
    register_kind(
        BrokerKind(id="e2e-trade", label="E2E trade", auth="attach",
                   execution=True, execution_modes=("demo", "live")),
        RealAccountAdapter if live else FakeAdapter,
        replace=True,
    )
    service = BrokerService(store, approvals=approvals, guardrails=guardrails)
    service.create_connection(
        kind="e2e-trade", label="E2E live" if live else "E2E demo",
        mode="live" if live else "demo",
        config={"maxMargin": 500, "magic": 99},
    )
    return service


def _spec():
    return S.parse_strategy({
        "strategy": {"id": "e2e_bot", "name": "E2E bot", "version": "1.0.0"},
        "universe": {"symbols": ["EURUSD"]},
        "timeframes": {"primary": "1m"},
        "rules": {
            "entry": {"long": {"conditions": [
                {"left": {"ref": "close"}, "op": ">", "right": {"value": 0}},
            ]}},
            "exit": {},
        },
        "position_sizing": {"type": "percent_equity", "value": 10},
    })


def _dataset():
    return D.build_dataset({
        "EURUSD": D.series_from_rows("EURUSD", "1m", F.bull_market("EURUSD", n=240),
                                     source="synthetic"),
    })


def _prepared(tmp_path, *, mode="demo"):
    ams = AgentMemoryStore(os.path.join(str(tmp_path), "quant.db"))
    store = QuantStore(ams, artifact_root=str(tmp_path / "art"))
    spec = _spec()
    store.save_strategy(spec)
    approval = store.request_approval("e2e_bot", "1.0.0")
    store.decide_approval(approval["id"], approve=True)
    result = E.run_backtest(spec, _dataset(), E.BacktestConfig())
    store.save_experiment(result, strategy_id="e2e_bot",
                          strategy_version="1.0.0", kind="backtest")

    broker = _broker(tmp_path, live=(mode == "live"))
    connection_id = broker.list_connections()[0]["id"]
    label = broker.get_connection(connection_id)["label"]
    if mode == "live":
        broker.arm_connection(connection_id, confirmation="ARM LIVE 7", ttl_seconds=600)
        confirmation = "LIVE TRIAL e2e_bot 1.0.0"
        max_margin = 100.0
    else:
        confirmation = f"MANDATE DEMO {label}"
        max_margin = 100.0
    live = LiveStrategyService(store, broker=broker)
    trial = live.prepare(
        strategy_id="e2e_bot", version="1.0.0", connection_id=connection_id,
        symbols=["EURUSD"], mode=mode, timeframe="M1", volume=0.01,
        stop_loss_points=30, take_profit_points=45, max_orders=4,
        max_margin=max_margin, ttl_seconds=3600, confirmation=confirmation,
    )
    dataset = _dataset()
    last_bar = dataset.series["EURUSD"].timestamps[-1]
    return store, live, broker, connection_id, trial, dataset, last_bar


def _definition(connection_id, trial_id, *, mode="demo", tier="l1", consent="default"):
    if consent == "default":
        consent = {
            "version": 1, "kind": "standing", "modes": [mode],
            "grantedAt": "2026-11-17T00:00:00Z",
            "expiresAt": "2099-01-01T00:00:00Z", "revokedAt": None,
            "autoRenew": True,
            "limits": {"symbols": ["EURUSD"], "maxOrdersPerDay": 4,
                       "maxMarginPerOrder": 100, "maxOpenPositions": 1},
            "phrase": "e2e",
        }
    return SimpleNamespace(
        id="aut_e2e",
        version=1,
        action=SimpleNamespace(type="run_strategy", config={
            "strategyId": "e2e_bot", "strategyVersion": "1.0.0", "mode": mode,
            "connectionId": connection_id, "symbols": ["EURUSD"],
            "dynamicSymbols": False, "timeframe": "M1", "volume": 0.01,
            "stopLossPoints": 30, "takeProfitPoints": 45, "maxOrdersPerDay": 4,
            "session": None, "trialId": trial_id, "dryRun": False,
            "consent": consent,
        }),
        execution_authority=SimpleNamespace(
            tier=tier, appetite="conservative", sizing_mode="risk_based",
            require_stop=True, max_open_positions=1, max_margin_per_order=100,
            cooldown_s=0, max_daily_loss_pct=3,
        ),
    )


def _service(store, live, broker):
    return SimpleNamespace(
        quant_store=store, live_strategy=live, brokers=broker,
        server_market_gateway=lambda: None,
    )


@pytest.mark.asyncio
async def test_a_demo_bot_places_exactly_one_order(tmp_path, monkeypatch):
    FakeAdapter.SUBMIT_CALLS = 0
    FakeAdapter.POSITIONS = []
    store, live, broker, connection_id, trial, dataset, last_bar = _prepared(tmp_path)

    async def fake_dataset(_gateway, _strategy, **_kwargs):
        return dataset

    monkeypatch.setattr("harness.quant.live.dataset_from_strategy", fake_dataset)
    monkeypatch.setattr(
        trading_runner, "_now",
        lambda: datetime.fromisoformat(last_bar.replace("Z", "+00:00"))
        + timedelta(seconds=30),
    )

    definition = _definition(connection_id, trial["id"], mode="demo", tier="l1")
    result = await run_strategy(
        _service(store, live, broker), definition, run_id="r1", execution_id="e1",
    )

    assert result.state == "completed", result.output
    decision = result.detail["decision"]
    assert decision["outcome"] in ("filled", "placed"), decision
    assert decision["action"] == "buy"
    assert decision["strategyId"] == "e2e_bot"
    assert FakeAdapter.SUBMIT_CALLS == 1
    ledger = broker.ledger_orders(connection_id, automation_id="aut_e2e")
    assert len(ledger) == 1
    assert ledger[0]["automationId"] == "aut_e2e"
    # Auto-renew may replace an expiring mandate before the order; the ledger
    # must carry the trial's CURRENT mandate, not the one it was prepared with.
    current = live.get(trial["id"])
    assert ledger[0]["mandateId"] == current["mandateId"]
    # The replacement is a real renewal: a new mandate exists and the old one
    # was revoked only after the replacement was written.
    mandates = {row["id"]: row for row in broker.list_mandates(connection_id)}
    assert current["mandateId"] in mandates
    if trial["mandateId"] != current["mandateId"]:
        assert mandates[trial["mandateId"]]["revokedAt"] is not None
    assert len(FakeAdapter.POSITIONS) == 1


@pytest.mark.asyncio
async def test_the_same_bar_never_places_a_second_order(tmp_path, monkeypatch):
    FakeAdapter.SUBMIT_CALLS = 0
    FakeAdapter.POSITIONS = []
    store, live, broker, connection_id, trial, dataset, last_bar = _prepared(tmp_path)

    async def fake_dataset(_gateway, _strategy, **_kwargs):
        return dataset

    monkeypatch.setattr("harness.quant.live.dataset_from_strategy", fake_dataset)
    monkeypatch.setattr(
        trading_runner, "_now",
        lambda: datetime.fromisoformat(last_bar.replace("Z", "+00:00"))
        + timedelta(seconds=30),
    )
    definition = _definition(connection_id, trial["id"])
    service = _service(store, live, broker)

    first = await run_strategy(service, definition, run_id="r1", execution_id="e1")
    assert first.state == "completed"
    assert FakeAdapter.SUBMIT_CALLS == 1

    second = await run_strategy(service, definition, run_id="r2", execution_id="e2")
    assert FakeAdapter.SUBMIT_CALLS == 1
    assert second.detail["decision"]["outcome"] in ("skipped", "none")


@pytest.mark.asyncio
async def test_a_live_bot_refuses_before_the_adapter_when_disarmed(tmp_path, monkeypatch):
    FakeAdapter.SUBMIT_CALLS = 0
    FakeAdapter.POSITIONS = []
    store, live, broker, connection_id, trial, dataset, last_bar = _prepared(
        tmp_path, mode="live",
    )
    # Disarm after the trial was prepared: the submit must refuse, not send.
    broker.connections.set_arm(connection_id, until=None)

    async def fake_dataset(_gateway, _strategy, **_kwargs):
        return dataset

    monkeypatch.setattr("harness.quant.live.dataset_from_strategy", fake_dataset)
    monkeypatch.setattr(
        trading_runner, "_now",
        lambda: datetime.fromisoformat(last_bar.replace("Z", "+00:00"))
        + timedelta(seconds=30),
    )
    monkeypatch.setattr(
        "harness.trade.authority.bot_definition",
        lambda _id: SimpleNamespace(version=1, execution_authority=SimpleNamespace(
            tier="l2", appetite="conservative", require_stop=True,
            max_open_positions=1, cooldown_s=0, max_margin_per_order=100,
        )),
    )

    definition = _definition(connection_id, trial["id"], mode="live", tier="l2")
    result = await run_strategy(
        _service(store, live, broker), definition, run_id="r1", execution_id="e1",
    )

    assert result.state == "blocked", result.output
    assert result.detail["decision"]["code"] == "LIVE_NOT_ARMED"
    assert FakeAdapter.SUBMIT_CALLS == 0
    assert FakeAdapter.POSITIONS == []
