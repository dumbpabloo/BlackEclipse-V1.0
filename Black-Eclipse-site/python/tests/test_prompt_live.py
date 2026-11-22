"""Opt-in live prompt smoke against a real provider (never part of CI).

Set:
    HARNESS_LIVE_PROMPT_TEST=1
    HARNESS_LIVE_PROVIDER=opencode-go
    HARNESS_LIVE_MODEL=deepseek-v4.1-flash
    HARNESS_LIVE_BASE_URL=https://opencode.ai/inference/go/openai/v1
    HARNESS_LIVE_API_KEY=<operator key>

Then:
    node scripts/test-python.js python/tests/test_prompt_live.py

Without the flag the test is SKIPPED, so the offline suite stays key-free.
The prompts and assertions mirror `prompt_smoke_v1.json`; this is the live tier,
graded by the same runner.
"""

from __future__ import annotations

import asyncio
import os
import tempfile

import pytest

from harness.evaluation.runner import EvaluationRunner, ServiceAgentInvoker
from harness.evaluation.store import EvaluationStore
from harness.evaluation.tasks import EvaluationTask
from harness.persistence import AgentMemoryStore
from harness.schemas import ModelSettings

pytestmark = pytest.mark.skipif(
    os.environ.get("HARNESS_LIVE_PROMPT_TEST", "").strip().lower() not in ("1", "true", "yes", "on"),
    reason="live prompt smoke is opt-in: set HARNESS_LIVE_PROMPT_TEST=1 and the provider env vars",
)

TASKS = [
    {
        "id": "live.ready.001", "name": "direct answer", "category": "core", "difficulty": "easy",
        "input": {"prompt": "Reply with the single word PLUGIN_READY."},
        "graders": [{"name": "semantic", "config": {"must_include": ["PLUGIN_READY"]}}],
    },
    {
        "id": "live.math.001", "name": "arithmetic", "category": "core", "difficulty": "easy",
        "input": {"prompt": "What is 6 times 7? Answer with the number only."},
        "graders": [{"name": "semantic", "config": {"must_include": ["42"]}}],
    },
    {
        "id": "live.stoploss.001", "name": "finance explanation", "category": "finance", "difficulty": "easy",
        "input": {"prompt": "In one sentence, explain what a stop-loss order does."},
        "graders": [{"name": "semantic", "config": {"must_include": ["stop", "loss"]}}],
    },
    {
        "id": "live.portfolio.001", "name": "tool awareness", "category": "tools", "difficulty": "easy",
        "input": {"prompt": "List three tools you can use to inspect a portfolio. Answer briefly."},
        "graders": [{"name": "semantic", "config": {"must_include": ["portfolio"]}}],
    },
]


def _settings() -> ModelSettings:
    return ModelSettings(
        provider=os.environ["HARNESS_LIVE_PROVIDER"],
        model=os.environ["HARNESS_LIVE_MODEL"],
        base_url=os.environ.get("HARNESS_LIVE_BASE_URL"),
        api_key=os.environ["HARNESS_LIVE_API_KEY"],
        session_id="prompt-live-smoke",
    )


def test_live_prompts_complete_and_pass():
    store = AgentMemoryStore(os.path.join(tempfile.mkdtemp(prefix="be-live-prompt-"), "agent-memory.db"))
    try:
        from harness.service import AgenticService

        service = AgenticService(store=store)
        runner = EvaluationRunner(
            store,
            invoker=ServiceAgentInvoker(service, _settings()),
            eval_store=EvaluationStore(store),
        )
        suite = asyncio.run(runner.run_suite(
            [EvaluationTask.from_dict(task) for task in TASKS],
            dataset_name="live-prompt-smoke",
            trials=1,
            persist=False,
        ))
        assert suite.measured_trials == len(TASKS), {
            result.task.id: result.trials[0].trajectory.outcome for result in suite.tasks
        }
        for result in suite.tasks:
            trial = result.trials[0]
            assert trial.passed, (result.task.id, trial.score.grader_results, trial.trajectory.outcome)
    finally:
        store.close()
