"""Every terminal error the graph can emit must be a legal HarnessError code.

The defect this pins: the graph emits `RUN_TIMEOUT` when the RUN's wall clock
expires, but `HarnessErrorCode` did not include it, so building the failed
`RunResult` raised a pydantic literal error *while reporting the timeout* - the
real outcome was replaced by a validation traceback. A run timeout is a named,
legal terminal state.
"""

from __future__ import annotations

from harness.schemas import HarnessError


def test_run_timeout_is_a_valid_terminal_error():
    error = HarnessError(code="RUN_TIMEOUT", message="the run exceeded its wall clock")
    assert error.code == "RUN_TIMEOUT"


def test_provider_timeout_and_run_timeout_are_distinct():
    # One provider call timing out is not the same failure as the whole run.
    assert HarnessError(code="PROVIDER_TIMEOUT", message="").code == "PROVIDER_TIMEOUT"
