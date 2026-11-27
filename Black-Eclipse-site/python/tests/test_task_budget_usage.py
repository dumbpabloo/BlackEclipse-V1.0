"""Team budgets: cost/runtime reach the root BudgetTracker at the task boundary."""

from __future__ import annotations

from types import SimpleNamespace

from harness.tasks.bootstrap import _budget_exhausted, _record_budget_usage


class _Budgets:
    def __init__(self) -> None:
        self.recorded: list[dict] = []
        self.remaining = {"tokens": 10, "cost": 10, "runtime": 10}

    def record_usage(self, root, **kwargs):
        self.recorded.append({"root": root, **kwargs})

    def snapshot(self, root):
        return {"remaining": self.remaining}


def _ctx(budgets: _Budgets):
    return SimpleNamespace(budgets=budgets), "root_run"


def test_cost_and_runtime_are_recorded():
    budgets = _Budgets()
    _record_budget_usage(
        _ctx(budgets),
        SimpleNamespace(metrics={
            "totalTokens": 12,
            "toolCalls": 2,
            "totalCost": 0.25,
            "runtimeMs": 4_000,
        }),
    )
    assert budgets.recorded == [{
        "root": "root_run",
        "tokens": 12,
        "tool_calls": 2,
        "cost": 0.25,
        "runtime_ms": 4_000,
    }]


def test_no_usage_means_no_write():
    budgets = _Budgets()
    _record_budget_usage(_ctx(budgets), SimpleNamespace(metrics={}))
    assert budgets.recorded == []


def test_exhausted_cost_or_runtime_stops_new_work():
    budgets = _Budgets()
    budgets.remaining = {"tokens": 10, "cost": 0, "runtime": 10}
    assert _budget_exhausted(_ctx(budgets)) is True
    budgets.remaining = {"tokens": 10, "cost": 10, "runtime": 0}
    assert _budget_exhausted(_ctx(budgets)) is True
    budgets.remaining = {"tokens": 10, "cost": 10, "runtime": 10}
    assert _budget_exhausted(_ctx(budgets)) is False
