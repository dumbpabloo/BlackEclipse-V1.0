"""The Computer Use evaluation suite: tasks, a runner, and pass@1.

A benchmark is a list of tasks with a deterministic checker. This module owns
the scoring, not the tasks: a task is `name + async run(session) -> bool`, so the
same runner measures the local Windows suite, an OSWorld episode, or a future
harness without a second scoring path.

The number it reports is honest by construction: a task that raises is a
failure, a `None` return is a failure, and there is no partial credit.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional


@dataclass
class EvalTask:
    name: str
    run: Callable[[], Awaitable[Optional[bool]]]
    tags: list[str] = field(default_factory=list)


@dataclass
class TaskResult:
    name: str
    passed: bool
    error: str = ""
    duration_ms: int = 0
    tags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name, "passed": self.passed, "error": self.error,
            "durationMs": self.duration_ms, "tags": list(self.tags),
        }


@dataclass
class SuiteReport:
    suite: str
    results: list[TaskResult] = field(default_factory=list)
    duration_ms: int = 0

    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def passed(self) -> int:
        return sum(1 for r in self.results if r.passed)

    @property
    def pass_at_1(self) -> float:
        return (self.passed / self.total) if self.total else 0.0

    def failures(self) -> list[TaskResult]:
        return [r for r in self.results if not r.passed]

    def to_dict(self) -> dict[str, Any]:
        return {
            "suite": self.suite, "total": self.total, "passed": self.passed,
            "passAt1": round(self.pass_at_1, 4), "durationMs": self.duration_ms,
            "results": [r.to_dict() for r in self.results],
        }

    def report(self) -> str:
        lines = [
            f"[{self.suite}] pass@1 = {self.passed}/{self.total} "
            f"({self.pass_at_1:.3f}) in {self.duration_ms} ms"
        ]
        for result in self.results:
            status = "PASS" if result.passed else "FAIL"
            detail = f" — {result.error}" if result.error else ""
            lines.append(f"  {status} {result.name} ({result.duration_ms} ms){detail}")
        return "\n".join(lines)


async def run_suite(tasks: list[EvalTask], *, suite: str = "computer-use") -> SuiteReport:
    """Run every task once, in order, and score it. No retries, no partial credit."""
    report = SuiteReport(suite=suite)
    started = time.monotonic()
    for task in tasks:
        task_started = time.monotonic()
        passed = False
        error = ""
        try:
            passed = bool(await task.run())
        except Exception as exc:  # noqa: BLE001 - a throwing task is a failure
            error = f"{type(exc).__name__}: {str(exc)[:300]}"
        report.results.append(TaskResult(
            name=task.name, passed=passed, error=error,
            duration_ms=int((time.monotonic() - task_started) * 1000),
            tags=list(task.tags),
        ))
    report.duration_ms = int((time.monotonic() - started) * 1000)
    return report


__all__ = ["EvalTask", "TaskResult", "SuiteReport", "run_suite"]
