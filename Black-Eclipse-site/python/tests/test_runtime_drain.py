"""Cooperative runtime drain: refuse new work, finish live runs, report honestly."""

from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException

from harness.routers.runtime import require_operator_accepting_runs
from harness.runtime_state import (
    begin_draining,
    is_draining,
    reset_draining,
    wait_for_idle,
)


class _Service:
    def __init__(self, active):
        self._active = active


def test_wait_for_idle_waits_then_reports_idle():
    service = _Service({"run_a": object()})

    async def _finish() -> None:
        await asyncio.sleep(0.2)
        service._active.clear()

    async def _run():
        task = asyncio.create_task(_finish())
        try:
            return await wait_for_idle(service, timeout_s=2.0)
        finally:
            await task

    status = asyncio.run(_run())
    assert status["idle"] is True
    assert status["activeRuns"] == 0


def test_wait_for_idle_reports_the_remaining_runs_on_timeout():
    service = _Service({"run_a": object(), "run_b": object()})
    status = asyncio.run(wait_for_idle(service, timeout_s=0.05))
    assert status["idle"] is False
    assert status["activeRuns"] == 2


def test_a_draining_runtime_refuses_new_admission():
    begin_draining()
    try:
        assert is_draining() is True
        with pytest.raises(HTTPException) as exc:
            require_operator_accepting_runs(True)
        assert exc.value.status_code == 503
    finally:
        reset_draining()
    assert require_operator_accepting_runs(True) is True
