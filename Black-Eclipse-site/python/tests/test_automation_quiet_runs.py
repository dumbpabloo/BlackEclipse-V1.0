"""Quiet scheduled runs + continuity handoff.

A scheduled bot is allowed to answer "nothing changed" without posting a fake
report, toasting the operator, or delivering a notification. It must still keep
the execution row, and a pending order must always break quiet.
"""

from __future__ import annotations

from typing import Any

import pytest

from harness.automation.dispatcher import (
    QUIET_MARKERS,
    Dispatcher,
    is_quiet_answer,
)
from harness.automation.model import AutomationDefinition


class _Result:
    def __init__(self, output: str) -> None:
        self._output = output

    def model_dump(self) -> dict[str, Any]:
        return {
            "status": "completed",
            "runId": "run_1",
            "output": self._output,
            "totalTokens": 7,
        }


class _Service:
    def __init__(self, output: str = "[[no-update]]") -> None:
        self.output = output
        self.tasks = None
        self.trade = None
        self.request = None
        self.history_rows = [
            {
                "state": "completed",
                "output": "Yesterday: the spread widened and I found nothing actionable.",
                "finishedAt": "2026-11-25T09:00:00Z",
            },
            {
                "state": "completed",
                "output": "[[no-update]]",
                "finishedAt": "2026-11-25T10:00:00Z",
            },
            {
                "state": "failed",
                "output": "provider error",
                "finishedAt": "2026-11-25T11:00:00Z",
            },
        ]

    def history(self, automation_id: str, *, limit: int = 100):
        assert automation_id == "auto_quiet"
        return self.history_rows[:limit]

    async def run(self, request):
        self.request = request
        return _Result(self.output)


def _definition(*, trigger_type: str = "interval") -> AutomationDefinition:
    return AutomationDefinition(
        id="auto_quiet",
        name="Quiet monitor",
        state="active",
        trigger={"type": trigger_type, "interval_seconds": 3600, "timezone": "UTC"},
        action={"type": "run_agent", "objective": "Watch the spread"},
        created_at="2026-11-26T00:00:00Z",
        updated_at="2026-11-26T00:00:00Z",
    )


def _dispatcher(service: _Service) -> Dispatcher:
    return Dispatcher(service)


def test_is_quiet_answer_requires_the_whole_answer():
    for marker in QUIET_MARKERS:
        assert is_quiet_answer(marker)
        assert is_quiet_answer(f"  {marker}\n")
    assert not is_quiet_answer("Nothing changed. [[no-update]]")
    assert not is_quiet_answer("")


def test_scheduled_run_gets_handoff_and_quiet_contract():
    service = _Service()
    request = _dispatcher(service)._run_request(_definition(), "run_1", "Watch the spread")
    query = request.input.query

    assert "Previous scheduled-run reports for continuity" in query
    # Prior real output is included ...
    assert "the spread widened" in query
    # ... but a quiet run and a failed run are not treated as history.
    assert "2026-11-25T10:00:00Z" not in query
    assert "provider error" not in query
    assert "reply with exactly [[no-update]]" in query


def test_manual_run_does_not_get_the_autonomous_contract():
    service = _Service()
    request = _dispatcher(service)._run_request(
        _definition(trigger_type="manual"), "run_1", "Watch the spread"
    )
    query = request.input.query

    assert "Previous scheduled-run reports" not in query
    assert "reply with exactly [[no-update]]" not in query


def test_default_bot_workspace_is_per_bot_and_inside_the_root(monkeypatch, tmp_path):
    monkeypatch.setenv("BLACK_ECLIPSE_CODE_ROOT", str(tmp_path))
    root = Dispatcher._default_bot_workspace("auto_quiet")
    assert root is not None
    path = tmp_path / ".black-eclipse" / "bots" / "auto_quiet"
    assert path.is_dir()
    assert root == str(path)


@pytest.mark.asyncio
async def test_quiet_run_is_marked_quiet():
    service = _Service()
    result = await _dispatcher(service)._run_agent(
        _definition(), "run_1", "exec_1"
    )
    assert result.state == "completed"
    assert result.detail.get("quiet") is True
    assert result.output == "[[no-update]]"


@pytest.mark.asyncio
async def test_pending_order_breaks_quiet(monkeypatch):
    service = _Service()
    dispatcher = _dispatcher(service)
    monkeypatch.setattr(
        dispatcher,
        "_pending_trade_approvals",
        lambda _run_id: [{"approvalId": "appr_1", "summary": "BUY EURUSD"}],
    )
    result = await dispatcher._run_agent(_definition(), "run_1", "exec_1")
    assert "quiet" not in result.detail
    assert result.detail["pendingApprovals"][0]["approvalId"] == "appr_1"
