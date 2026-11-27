"""Chat step budget (QA Round 1, Q5).

8 model steps was too tight for a multi-tool navigation turn; the QA run hit the
cap mid-task, and 12 was too tight for a request that carries several
deliverables (fetch -> compute -> chart -> report -> widget): the run spent its
budget on the data work and never produced the file. Ordinary chat now defaults
to 24, an operator can set `ECLIPSE_CHAT_MAX_STEPS` (1..32), and an explicit
per-run agent config wins.
"""

from __future__ import annotations

from types import SimpleNamespace

from harness.agents import CODE_AGENT, ECLIPSE_AGENT
from harness.graph import DEFAULT_CHAT_RUN_TIMEOUT_S, chat_run_budget, chat_step_budget


def _req(explicit=None):
    return SimpleNamespace(agent=explicit)


def test_default_chat_budget_is_twenty_four(monkeypatch):
    monkeypatch.delenv("ECLIPSE_CHAT_MAX_STEPS", raising=False)
    assert chat_step_budget(ECLIPSE_AGENT, _req()) == 24


def test_env_override_and_clamp(monkeypatch):
    monkeypatch.setenv("ECLIPSE_CHAT_MAX_STEPS", "20")
    assert chat_step_budget(ECLIPSE_AGENT, _req()) == 20
    monkeypatch.setenv("ECLIPSE_CHAT_MAX_STEPS", "999")
    assert chat_step_budget(ECLIPSE_AGENT, _req()) == 32
    monkeypatch.setenv("ECLIPSE_CHAT_MAX_STEPS", "0")
    assert chat_step_budget(ECLIPSE_AGENT, _req()) == 1


def test_invalid_env_is_ignored(monkeypatch):
    monkeypatch.setenv("ECLIPSE_CHAT_MAX_STEPS", "not-a-number")
    assert chat_step_budget(ECLIPSE_AGENT, _req()) == 24


def test_deep_quant_turn_gets_the_strategy_build_floor(monkeypatch):
    """A strategy/model build is a research cycle, not chat navigation."""
    monkeypatch.delenv("ECLIPSE_CHAT_MAX_STEPS", raising=False)
    request = SimpleNamespace(
        agent=None,
        input=SimpleNamespace(
            query="build me a strategy and backtest it", history=None,
        ),
    )
    assert chat_step_budget(ECLIPSE_AGENT, request) == 32
    # The operator's explicit budget still wins over the deep-quant floor.
    monkeypatch.setenv("ECLIPSE_CHAT_MAX_STEPS", "20")
    assert chat_step_budget(ECLIPSE_AGENT, request) == 20


def test_explicit_agent_config_wins(monkeypatch):
    monkeypatch.setenv("ECLIPSE_CHAT_MAX_STEPS", "20")
    explicit = ECLIPSE_AGENT.model_copy(update={"maxSteps": 6})
    assert chat_step_budget(explicit, _req(explicit=explicit)) == 6


def test_other_agents_are_unchanged(monkeypatch):
    monkeypatch.setenv("ECLIPSE_CHAT_MAX_STEPS", "20")
    assert chat_step_budget(CODE_AGENT, _req()) == (CODE_AGENT.maxSteps or 8)


# ── Run wall clock (a multi-step turn cannot fit one model call) ─────────────


def test_default_chat_run_clock_gets_a_multi_step_floor(monkeypatch):
    """`runTimeoutMs` is a PER-CALL budget; a 24-step turn needs more.

    Using 120s as the whole-run deadline made every real research turn fail with
    RUN_TIMEOUT before it could answer.
    """
    monkeypatch.delenv("ECLIPSE_CHAT_RUN_TIMEOUT_S", raising=False)
    assert chat_run_budget(ECLIPSE_AGENT, _req()) == DEFAULT_CHAT_RUN_TIMEOUT_S
    assert DEFAULT_CHAT_RUN_TIMEOUT_S >= 600.0


def test_chat_run_clock_is_operator_tunable_and_clamped(monkeypatch):
    monkeypatch.setenv("ECLIPSE_CHAT_RUN_TIMEOUT_S", "900")
    assert chat_run_budget(ECLIPSE_AGENT, _req()) == 900.0
    # The floor may only RAISE the wall clock; the preset's 120s still applies.
    monkeypatch.setenv("ECLIPSE_CHAT_RUN_TIMEOUT_S", "1")
    assert chat_run_budget(ECLIPSE_AGENT, _req()) == 120.0
    monkeypatch.setenv("ECLIPSE_CHAT_RUN_TIMEOUT_S", "not-a-number")
    assert chat_run_budget(ECLIPSE_AGENT, _req()) == DEFAULT_CHAT_RUN_TIMEOUT_S


def test_an_explicit_agent_config_keeps_its_own_run_clock():
    explicit = ECLIPSE_AGENT.model_copy(update={"runTimeoutMs": 120_000})
    assert chat_run_budget(explicit, _req(explicit=explicit)) == 120.0
    assert chat_run_budget(CODE_AGENT, _req()) == (CODE_AGENT.runTimeoutMs or 120_000) / 1000
