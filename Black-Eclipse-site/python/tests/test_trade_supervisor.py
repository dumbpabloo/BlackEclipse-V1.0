"""The trading supervisor: portfolio stops and automatic bot stand-down.

All figures are asserted as arithmetic from broker/ledger facts: a book 3% below
its day-open MUST block at a 2% daily stop, and a bot with 30 losing fills and no
profit MUST be paused. An unreadable account is `unmeasured`, never "healthy".
"""

from __future__ import annotations

import pytest

from harness.trade.supervisor import (
    SupervisorLimits,
    TradeSupervisor,
    assess_bot,
    assess_portfolio,
    limits_from_config,
)


def _accounts(*rows):
    return list(rows)


def _account(equity=10_000, margin=0, positions=0):
    return {"equity": equity, "balance": equity, "margin": margin, "positions": positions}


# -- portfolio ----------------------------------------------------------------


def test_a_flat_book_is_ok():
    report = assess_portfolio(
        accounts=_accounts(_account()),
        day_start_equity=10_000,
        week_start_equity=10_000,
    )
    assert report["verdict"] == "ok"
    assert report["blocking"] is False


def test_the_daily_stop_blocks_at_the_threshold():
    report = assess_portfolio(
        accounts=_accounts(_account(equity=9_700)),
        day_start_equity=10_000,
        week_start_equity=10_000,
    )
    assert report["verdict"] == "block"
    assert report["dailyLossPct"] == pytest.approx(3.0)
    assert any("today" in reason for reason in report["reasons"])


def test_the_weekly_stop_blocks_even_on_a_flat_day():
    report = assess_portfolio(
        accounts=_accounts(_account(equity=9_400)),
        day_start_equity=9_400,
        week_start_equity=10_000,
    )
    assert report["verdict"] == "block"
    assert any("this week" in reason for reason in report["reasons"])


def test_a_gain_is_never_a_loss():
    report = assess_portfolio(
        accounts=_accounts(_account(equity=11_000)),
        day_start_equity=10_000,
        week_start_equity=10_000,
    )
    assert report["dailyLossPct"] == 0.0
    assert report["verdict"] == "ok"


def test_margin_and_position_count_block():
    report = assess_portfolio(
        accounts=_accounts(_account(margin=7_000, positions=20)),
        day_start_equity=10_000,
        week_start_equity=10_000,
    )
    assert report["verdict"] == "block"
    assert any("margin" in reason for reason in report["reasons"])
    assert any("positions" in reason for reason in report["reasons"])


def test_near_the_margin_cap_is_a_warning_not_a_block():
    report = assess_portfolio(
        accounts=_accounts(_account(margin=5_200)),
        day_start_equity=10_000,
        week_start_equity=10_000,
    )
    assert report["verdict"] == "warn"
    assert report["blocking"] is False


def test_missing_baselines_are_unmeasured_not_passed():
    report = assess_portfolio(
        accounts=_accounts(_account()), day_start_equity=None, week_start_equity=None,
    )
    assert report["verdict"] == "ok"
    assert any("day loss" in item for item in report["unmeasured"])
    assert any("week loss" in item for item in report["unmeasured"])


def test_no_equity_is_unmeasured():
    report = assess_portfolio(
        accounts=[{"equity": 0, "margin": 0, "positions": 0}],
        day_start_equity=10_000, week_start_equity=10_000,
    )
    assert report["marginPct"] is None
    assert any("equity" in item for item in report["unmeasured"])


def test_limits_are_read_from_config_without_widening_on_garbage():
    limits = limits_from_config(
        {"supervisor": {"dailyLossPct": "nonsense", "maxPortfolioPositions": -5}}
    )
    assert limits.daily_loss_pct == 2.0
    assert limits.max_portfolio_positions == 15


# -- bots ---------------------------------------------------------------------


def _fills(*profits):
    return [
        {"profit": p, "commission": 0, "swap": 0, "dealtAt": i, "orderTicket": 1}
        for i, p in enumerate(profits)
    ]


def test_a_new_bot_is_insufficient_not_failed():
    report = assess_bot(
        automation_id="bot_a", orders=[{"ticket": 1}], fills=_fills(-1, -2),
    )
    assert report["verdict"] == "insufficient"
    assert report["trades"] == 2


def test_a_bot_with_enough_trades_and_no_profit_is_paused():
    fills = _fills(*([-1] * 30))
    report = assess_bot(automation_id="bot_a", orders=[{"ticket": 1}], fills=fills)
    assert report["verdict"] == "pause"
    assert report["realizedPnl"] < 0
    assert any("not earned" in reason for reason in report["reasons"])


def test_a_profitable_bot_is_kept():
    fills = _fills(*([2] * 30))
    report = assess_bot(automation_id="bot_a", orders=[{"ticket": 1}], fills=fills)
    assert report["verdict"] == "ok"


def test_commission_and_swap_count_against_the_bot():
    fills = [{"profit": 1, "commission": 1.5, "swap": 0.5, "dealtAt": i} for i in range(30)]
    report = assess_bot(automation_id="bot_a", orders=[{"ticket": 1}], fills=fills)
    assert report["realizedPnl"] == pytest.approx(-30.0)
    assert report["verdict"] == "pause"


def test_a_losing_streak_pauses_before_the_trade_count():
    report = assess_bot(
        automation_id="bot_a", orders=[{"ticket": 1}],
        fills=_fills(*([1] * 4 + [-1] * 8)),
        limits=SupervisorLimits(bot_min_trades=100, bot_max_consecutive_losses=5),
    )
    assert report["verdict"] == "pause"
    assert report["consecutiveLosses"] == 8


# -- the service facade -------------------------------------------------------


class _Connections:
    def __init__(self):
        self.snapshots: dict[tuple[str, str], dict] = {}
        self.fill_rows: list[dict] = []

    def list(self):
        return [type("C", (), {"id": "c1", "config": {"supervisor": {"dailyLossPct": 2}}})()]

    def set_risk_snapshot(self, scope, period, equity, started_at):
        self.snapshots.setdefault((scope, period), {
            "scope": scope, "period": period, "equity": equity, "startedAt": started_at,
        })

    def get_risk_snapshot(self, scope, period):
        return self.snapshots.get((scope, period))

    def list_fills(self, connection_id, limit=500):
        return self.fill_rows


class _Broker:
    def __init__(self, equity=10_000):
        self.connections = _Connections()
        self.equity = equity
        self.orders = [{
            "automationId": "bot_a", "connectionId": "c1", "ticket": 111,
            "status": "filled", "createdAt": "2026-11-22",
        }]

    def account(self, connection_id):
        return {"available": True, "data": {"equity": self.equity, "balance": self.equity,
                                            "margin": 0, "marginFree": self.equity}}

    def positions(self, connection_id):
        return {"available": True, "data": []}

    def ledger_orders(self, limit=500):
        return self.orders


class _Automations:
    def __init__(self):
        self.paused: list[str] = []

    def get(self, automation_id):
        return type("D", (), {"id": automation_id, "state": "active"})()

    def pause(self, automation_id, *, actor="operator"):
        self.paused.append(automation_id)
        return type("D", (), {"id": automation_id, "state": "paused"})()


def test_the_supervisor_sets_the_day_baseline_once_and_blocks_after_a_loss():
    broker = _Broker()
    supervisor = TradeSupervisor(broker)
    first = supervisor.portfolio()
    assert first["verdict"] == "ok"
    assert first["dayStartEquity"] == pytest.approx(10_000)

    broker.equity = 9_600  # -4% from the baseline
    second = supervisor.portfolio()
    assert second["verdict"] == "block"
    assert second["dailyLossPct"] == pytest.approx(4.0)
    assert second["dayStartEquity"] == pytest.approx(10_000)  # baseline never moved


def test_apply_pauses_only_the_bot_that_should_stand_down():
    broker = _Broker()
    broker.connections.fill_rows = [
        {"profit": -1, "commission": 0, "swap": 0, "dealtAt": i, "orderTicket": 111}
        for i in range(30)
    ]
    automations = _Automations()
    result = TradeSupervisor(broker, automations=automations).apply()
    assert result["paused"] == ["bot_a"]
    assert automations.paused == ["bot_a"]


def test_apply_is_idempotent_for_an_already_paused_bot():
    broker = _Broker()
    broker.connections.fill_rows = [
        {"profit": -1, "commission": 0, "swap": 0, "dealtAt": i, "orderTicket": 111}
        for i in range(30)
    ]

    class AlreadyPaused(_Automations):
        def get(self, automation_id):
            return type("D", (), {"id": automation_id, "state": "paused"})()

    automations = AlreadyPaused()
    result = TradeSupervisor(broker, automations=automations).apply()
    assert result["paused"] == []
    assert automations.paused == []


def test_apply_without_the_automation_service_reports_the_failure():
    broker = _Broker()
    broker.connections.fill_rows = [
        {"profit": -1, "commission": 0, "swap": 0, "dealtAt": i, "orderTicket": 111}
        for i in range(30)
    ]
    result = TradeSupervisor(broker, automations=None).apply()
    assert result["paused"] == []
    assert result["failed"][0]["automationId"] == "bot_a"


def test_the_report_combines_portfolio_and_bots():
    broker = _Broker()
    broker.connections.fill_rows = [
        {"profit": -1, "commission": 0, "swap": 0, "dealtAt": i, "orderTicket": 111}
        for i in range(30)
    ]
    report = TradeSupervisor(broker).report()
    assert report["portfolio"]["verdict"] in {"ok", "warn"}
    assert report["standingDown"] == ["bot_a"]


def test_the_tools_are_registered_with_the_right_permissions():
    from harness.trade.tools import (
        BROKER_TOOLS,
        register_broker_tools,
        register_supervisor_tools,
    )
    from harness.tools import ToolRegistry

    registry = ToolRegistry()
    register_broker_tools(registry)
    assert "risk_supervisor" in BROKER_TOOLS
    assert registry.get("risk_supervisor").permission == "read"
    # The stand-down is a state change: it must NOT be in the read-only broker
    # contract, and it is registered separately as write/high.
    assert "risk_supervisor_apply" not in set(registry._tools)  # noqa: SLF001
    register_supervisor_tools(registry)
    assert registry.get("risk_supervisor_apply").permission == "write"
    assert registry.get("risk_supervisor_apply").risk == "high"
