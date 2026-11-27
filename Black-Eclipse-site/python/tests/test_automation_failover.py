"""Per-bot backup model: provider-class failures retry once; policy errors do not."""

from __future__ import annotations

import pytest

from harness.automation.dispatcher import Dispatcher
from harness.automation.model import AutomationDefinition


class _Result:
    def __init__(self, status: str, *, output: str = "", code: str = "") -> None:
        self._status = status
        self._output = output
        self._code = code

    def model_dump(self):
        return {
            "status": self._status,
            "runId": "run_1",
            "output": self._output,
            "totalTokens": 1,
            "error": {"code": self._code, "message": self._code} if self._code else None,
        }


class _Service:
    tasks = None
    trade = None

    def __init__(self, results):
        self._results = list(results)
        self.calls: list[str] = []

    async def run(self, request):
        self.calls.append(request.modelSettings.model)
        return self._results.pop(0)


def _definition(backup: bool = True) -> AutomationDefinition:
    config = {
        "backupModelSettings": {"provider": "openai", "model": "backup-model"},
    } if backup else {}
    return AutomationDefinition(
        id="auto_failover",
        name="Failover bot",
        state="active",
        trigger={"type": "manual"},
        action={"type": "run_agent", "objective": "brief", "config": config},
        created_at="2026-11-26T00:00:00Z",
        updated_at="2026-11-26T00:00:00Z",
    )


@pytest.mark.asyncio
async def test_provider_failure_retries_on_backup():
    service = _Service([
        _Result("failed", code="PROVIDER_TIMEOUT"),
        _Result("completed", output="backup answer"),
    ])
    result = await Dispatcher(service)._run_agent(_definition(), "run_1", "exec_1")
    assert service.calls == ["llama3.1", "backup-model"]
    assert result.state == "completed"
    assert result.output == "backup answer"
    assert result.detail.get("backupModelUsed") is True


@pytest.mark.asyncio
async def test_permission_failure_is_not_retried():
    service = _Service([_Result("failed", code="PERMISSION_DENIED")])
    result = await Dispatcher(service)._run_agent(_definition(), "run_1", "exec_1")
    assert service.calls == ["llama3.1"]
    assert result.state == "failed"
    assert "backupModelUsed" not in result.detail


@pytest.mark.asyncio
async def test_no_backup_means_one_call():
    service = _Service([_Result("failed", code="PROVIDER_TIMEOUT")])
    result = await Dispatcher(service)._run_agent(_definition(backup=False), "run_1", "exec_1")
    assert service.calls == ["llama3.1"]
    assert result.state == "failed"
