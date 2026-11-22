"""Branch parity, eighth tier: what counts as an order, in a goal and in a mandate.

Two references fetched 2026-11-21 (MIT, `HKUDS/Vibe-Trading`):

* `goal/policy.py::reject_live_execution_objective` — the seven patterns that make a
  research objective an execution request, and the two deliberate non-matches
  ("long-term"/"short-term" are horizons; an asset noun alone is not a cue). Eclipse
  had **no guard at all**, so "Buy 100 shares of NVDA now" was an accepted goal.
* `live/enforcement.py::check_mandate` — every check is **fail-closed**: an
  unparseable intent, unreadable positions or missing market data denies the order.
  Eclipse's pre-trade policy skipped each cap when its field was missing, so a broker
  preview that omitted `margin` walked past a configured margin cap. Fixed here and
  pinned.

Where the two designs legitimately differ, the test says so instead of pretending:
Eclipse's `TradePolicy.evaluate` returns **every** reason it can find (so one response
can list all the breaches), while the reference returns the **first** breach with a
`kind` that routes it to DENY or PAUSE. The shared part — the decisions — is what this
file asserts.
"""

from __future__ import annotations

import pytest

from harness.goals.policy import ExecutionObjectiveError, reject_execution_objective
from harness.trade.policy import TradePolicy


# -- goal objectives: the reference's accept/reject table -----------------

REJECTED_OBJECTIVES = [
    "Buy NVDA",
    "Please sell my TSLA",
    "Buy NVDA now",
    "immediately buy TSLA",
    "right away sell AAPL",
    "then buy 100 shares of NVDA",
    "short 2 ES",
    "place a market order for AAPL",
    "submit the trade for review",
    "execute order 42",
    "马上买 NVDA",
    "立即买入 100 股",
    "下单买入 100 股",
]

ACCEPTED_OBJECTIVES = [
    "evaluate whether to buy NVDA",
    "build a long-term thesis on MSFT",
    "update the long-term thesis immediately",
    "is the short-term momentum in TSLA decaying",
    "a short thesis on GME",
    "Short interest in GME is high",
    "research the best way to sell covered calls",
    "compare AAPL and MSFT valuations",
    "write a note on when to buy quality at a discount",
]


@pytest.mark.parametrize("objective", REJECTED_OBJECTIVES)
def test_orders_are_refused(objective):
    with pytest.raises(ExecutionObjectiveError):
        reject_execution_objective(objective)


@pytest.mark.parametrize("objective", ACCEPTED_OBJECTIVES)
def test_research_objectives_pass(objective):
    reject_execution_objective(objective)      # must not raise


def test_the_two_horizon_qualifiers_are_not_verbs():
    """`long` is a prefix of `long-term`: a word boundary alone would misfire."""
    reject_execution_objective("update the long-term thesis immediately")
    reject_execution_objective("write about long term value immediately")
    # but a plain verb with the cue still fires
    with pytest.raises(ExecutionObjectiveError):
        reject_execution_objective("sell the position immediately")


def test_the_refusal_names_the_fix():
    with pytest.raises(ExecutionObjectiveError) as exc:
        reject_execution_objective("Buy NVDA")
    assert "research question" in str(exc.value)
    assert "never places orders" in str(exc.value)


def test_goal_creation_refuses_an_order_before_dispatching_a_task():
    """The guard sits at the creation seam, not in the middle of a run."""
    from harness.goals.goal_runtime import GoalRuntime

    class _Exploding:
        def create(self, *_a, **_k):          # pragma: no cover - must not be called
            raise AssertionError("a task was created for an execution goal")

    runtime = GoalRuntime.__new__(GoalRuntime)
    runtime.tasks = _Exploding()
    with pytest.raises(Exception) as exc:
        runtime.create("Buy 100 shares of NVDA now")
    assert "EXECUTION_OBJECTIVE" in str(exc.value) or "execution goals" in str(exc.value)


# -- pre-trade policy: fail closed on a cap that cannot be verified -------


def _preview(**overrides):
    base = {
        "symbol": "NVDA",
        "volume": 1.0,
        "price": 100.0,
        "stopLoss": 95.0,
        "notional": 100.0,
        "contractSize": 1.0,
        "margin": 100.0,
        "spreadPoints": 1.0,
        "openPositions": 0,
        "account": {"equity": 10_000.0},
    }
    base.update(overrides)
    return base


@pytest.mark.parametrize("cap_kwarg,field", [
    ("max_margin", "margin"),
    ("max_volume", "volume"),
    ("max_symbol_notional", "notional"),
    ("max_open_positions", "openPositions"),
])
def test_a_configured_cap_refuses_when_its_field_is_missing(cap_kwarg, field):
    """The reference's fail-closed contract: unverifiable is not allowed."""
    policy = TradePolicy(**{cap_kwarg: 5})
    preview = _preview()
    preview.pop(field)
    result = policy.evaluate(preview)
    assert result["allowed"] is False
    assert any(rule.endswith("-unverified") for rule in result["rules"]), result["rules"]


def test_the_same_cap_still_allows_a_verifiable_order():
    policy = TradePolicy(max_margin=1000.0)
    result = policy.evaluate(_preview())
    assert result["allowed"] is True


def test_an_excluded_symbol_is_denied_the_way_the_mandate_exclude_list_denies_it():
    policy = TradePolicy(symbol_allowlist=("MSFT",))
    assert policy.evaluate(_preview())["allowed"] is False


def test_an_oversized_single_order_is_denied_the_way_the_notional_cap_denies_it():
    policy = TradePolicy(max_symbol_notional=50.0)
    result = policy.evaluate(_preview(notional=500.0))
    assert result["allowed"] is False
    assert "policy.max-symbol-notional" in result["rules"]


def test_every_breach_is_reported_not_just_the_first():
    """Eclipse lists all reasons (the reference stops at the first breach)."""
    policy = TradePolicy(symbol_allowlist=("MSFT",), max_margin=10.0,
                         max_symbol_notional=10.0)
    rules = policy.evaluate(_preview())["rules"]
    assert "policy.symbol-not-allowed" in rules
    assert "policy.max-margin" in rules
    assert "policy.max-symbol-notional" in rules


def test_a_zero_cap_means_off_not_fail_closed():
    """`0` disables a rule; it must not turn every order into a refusal."""
    policy = TradePolicy(max_open_positions=0, max_spread_points=0)
    assert policy.evaluate({"symbol": "NVDA"})["allowed"] is True


def test_the_default_position_cap_refuses_an_unreadable_count():
    """The reference's contract on the default: 20 positions, and unknown denies."""
    result = TradePolicy().evaluate({"symbol": "NVDA", "margin": 1.0})
    assert result["allowed"] is False
    assert "policy.max-open-positions-unverified" in result["rules"]
