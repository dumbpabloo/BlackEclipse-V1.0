"""Method discovery: library, fit ranking, structure, synthesis, mining, persistence."""

from __future__ import annotations

import math

import numpy as np
import pytest

from harness.quant import fixtures as F
from harness.quant.discovery.diagnostics import analyze_structure
from harness.quant.discovery.hypotheses import generate_hypotheses
from harness.quant.discovery.library import (
    get_method_card,
    list_method_families,
    recommend_methods,
)
from harness.quant.discovery.miner import mine_methods
from harness.quant.discovery.synthesize import (
    build_combination,
    build_strategy,
    synthesize_method,
)
from harness.quant.discovery.taxonomy import FAMILIES
from harness.quant.errors import QuantError
from harness.quant.store import QuantStore
from harness.persistence import AgentMemoryStore


def test_library_covers_the_method_families():
    ids = {m["id"] for m in list_method_families()}
    assert len(FAMILIES) >= 12
    assert {"trend_ema_cross", "mean_reversion_bollinger", "momentum_cross_sectional",
            "pairs_cointegration", "carry_futures", "ml_direction",
            "options_income", "seasonality_calendar"} <= ids


def test_recommend_methods_is_explainable_and_ranked():
    result = recommend_methods({
        "assetClass": "equities", "horizon": "swing", "objective": "growth",
        "riskTolerance": "medium", "symbol": "AAPL",
    }, limit=4)
    recs = result["recommendations"]
    assert len(recs) == 4
    assert recs[0]["fitScore"] >= recs[-1]["fitScore"]
    assert recs[0]["reasons"]
    assert "starterSpec" in result


def test_method_card_has_playbook_and_example():
    card = get_method_card("trend_ema_cross", symbol="AAPL")
    assert "playbook" in card
    assert card["playbook"]["where_it_fails"]
    assert card["exampleSpec"]["strategy"]["id"]


def test_synthesize_each_supported_builder_is_valid():
    for method_id in ("trend_ema_cross", "trend_donchian_breakout",
                      "mean_reversion_bollinger", "mean_reversion_rsi",
                      "volatility_breakout"):
        result = synthesize_method(method_id, symbol="AAPL")
        assert result["valid"], (method_id, result["issues"])


def test_synthesized_futures_symbol_is_valid():
    """A gold futures symbol must not fail on its asset class.

    `infer_asset_class` used to return "futures" while the validator's
    SUPPORTED_ASSET_CLASSES spells it "future", so every GC=F synthesis came
    back valid=False and the build stopped before a backtest.
    """
    result = synthesize_method("trend_donchian_breakout", symbol="GC=F")
    assert result["valid"], result["issues"]
    assert result["spec"]["asset_class"] == "future"


def test_non_spec_methods_refuse_honestly():
    with pytest.raises(QuantError):
        build_strategy("momentum_cross_sectional", symbol="AAPL")


def test_playbook_renders_the_actual_rules():
    from harness.quant.playbook import build_playbook

    spec = build_strategy("trend_ema_cross", symbol="AAPL")
    book = build_playbook(spec, symbol="AAPL",
                          method_family={"thesis": "trend", "failure_modes": ["whipsaw"]})
    assert book["entry"] and book["exit"]
    assert book["positionSizing"]
    assert book["whenItFails"] == ["whipsaw"]
    assert book["execution"]["commission"]
    assert "not investment advice" in book["disclaimer"].lower()


def test_combine_methods_produces_a_new_valid_spec():
    spec = build_combination("trend_ema_cross", "volatility_breakout", symbol="AAPL")
    assert spec["derivedFrom"] == ["trend_ema_cross", "volatility_breakout"]
    assert spec["strategy"]["id"] != "trend_ema_cross"


def test_structure_identifies_trend_and_mean_reversion():
    trend = analyze_structure(F.bull_market("T", n=260))
    assert trend["structure"] in ("trending", "drifting")
    assert trend["candidateMethods"]

    rng = np.random.default_rng(21)
    rows = []
    x = 0.0
    for i in range(260):
        x = 0.7 * x + float(rng.normal(0, 0.01))
        px = 100 * math.exp(x)
        rows.append({"timestamp": f"2024-{1 + (i // 28) % 12:02d}-{1 + i % 28:02d}",
                     "open": px, "high": px * 1.01, "low": px * 0.99,
                     "close": px, "volume": 1000})
    mean_rev = analyze_structure(rows)
    assert mean_rev["structure"] in ("mean_reverting", "volatility_clustered")
    assert mean_rev["metrics"]["meanReversionHalfLifeBars"] is not None


def test_hypotheses_have_acceptance_and_invalidation():
    diag = analyze_structure(F.bull_market("T", n=260))
    result = generate_hypotheses(diag, symbol="T")
    assert result["hypotheses"]
    for h in result["hypotheses"]:
        assert h["acceptance"] and h["invalidation"] and h["test"]


def test_miner_is_bounded_and_reports_real_trials():
    rows = F.bull_market("MINE", n=320)
    result = mine_methods(rows, symbol="MINE", max_trials=6, oos_fraction=0.3)
    assert result["trialCount"] <= 6
    assert result["isBars"] + result["oosBars"] == 320
    assert "allTrials" in result and "deflatedSharpe" in result


def test_discovered_methods_persist(tmp_path):
    store = QuantStore(AgentMemoryStore(str(tmp_path / "am.db")),
                       artifact_root=str(tmp_path / "art"))
    method = store.save_method(
        name="EMA + breakout filter", method_family="trend_ema_cross",
        spec=build_strategy("trend_ema_cross", symbol="AAPL"),
        evidence={"trialCount": 6, "oosSharpe": 1.1},
    )
    assert method["status"] == "discovered"
    assert store.list_methods()[0]["id"] == method["id"]
    promoted = store.promote_method(method["id"], "validated")
    assert promoted["status"] == "validated"
    with pytest.raises(QuantError):
        store.promote_method(method["id"], "made_up")
