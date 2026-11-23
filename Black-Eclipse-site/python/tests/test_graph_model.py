"""Graph runtime — IR contracts, config validation and the safe formula language."""

from __future__ import annotations

import pytest

from harness.graph_runtime.model import PortType, WorkflowIR
from harness.graph_runtime.nodes import FormulaError, build_default_registry, evaluate_formula, render_template
from harness.graph_runtime.registry import validate_config, validate_port_value


def _ir(**overrides):
    base = {
        "irVersion": 1,
        "id": "wf_test",
        "name": "Test",
        "description": "",
        "meta": {"createdAt": "", "updatedAt": "", "tags": []},
        "nodes": [],
        "edges": [],
        "settings": {"executionMode": "research", "failFast": False, "maxConcurrency": 1, "timeoutMs": 0},
        "requiredCapabilities": [],
    }
    base.update(overrides)
    return WorkflowIR.model_validate(base)


# ── IR parsing ──────────────────────────────────────────────────────────────


def test_ir_parses_camel_case_fields():
    ir = _ir(nodes=[{
        "id": "a", "type": "trigger.manual", "version": "1.0.0",
        "config": {"seed": "AAPL"}, "layout": {"x": 1, "y": 2},
    }], edges=[{
        "id": "e1", "source": "a", "sourceHandle": "start",
        "target": "b", "targetHandle": "value",
    }])
    assert ir.nodes[0].config["seed"] == "AAPL"
    assert ir.edges[0].source_handle == "start"
    assert ir.edges[0].target_handle == "value"
    assert ir.settings.execution_mode.value == "research"
    assert ir.model_dump(by_alias=True)["irVersion"] == 1


def test_ir_rejects_missing_required_fields():
    with pytest.raises(Exception):
        WorkflowIR.model_validate({"id": "x"})


# ── Port value validation ───────────────────────────────────────────────────


def test_primitive_port_validation():
    assert validate_port_value(PortType.NUMBER, 3) is None
    assert validate_port_value(PortType.NUMBER, True) is not None  # bool is not a number
    assert validate_port_value(PortType.STRING, "x") is None
    assert validate_port_value(PortType.STRING, 3) is not None
    assert validate_port_value(PortType.BOOLEAN, False) is None
    assert validate_port_value(PortType.OBJECT, {}) is None
    assert validate_port_value(PortType.ARRAY, []) is None
    assert validate_port_value(PortType.DATETIME, "2026-01-02T03:04:05Z") is None
    assert validate_port_value(PortType.DATETIME, "not-a-date") is not None
    assert validate_port_value(PortType.ANY, object()) is None
    assert validate_port_value(PortType.NUMBER, None) is None  # null always allowed


def test_domain_port_validation_is_structural():
    assert validate_port_value(PortType.OHLCV, [{"o": 1, "h": 2, "l": 0, "c": 1}]) is None
    assert validate_port_value(PortType.OHLCV, 12) is not None
    assert validate_port_value(PortType.INDICATOR, [1.0, 2.0]) is None
    assert validate_port_value(PortType.INDICATOR, {"values": [1, 2]}) is None
    assert validate_port_value(PortType.INDICATOR, "sma") is not None
    assert validate_port_value(PortType.INSTRUMENT, {"symbol": "AAPL"}) is None
    assert validate_port_value(PortType.INSTRUMENT, "AAPL") is not None
    assert validate_port_value(PortType.ORDER_INTENT, {"symbol": "AAPL", "side": "buy"}) is None
    assert validate_port_value(PortType.FILE, {"assetId": "gas_1", "name": "a.csv"}) is None
    assert validate_port_value(PortType.IMAGE, {"assetId": "img_1", "width": 10}) is None
    assert validate_port_value(PortType.FILE, "gas_1") is not None
    assert validate_port_value(PortType.IMAGE, {}) is not None


# ── Registry + config ───────────────────────────────────────────────────────


def test_every_registered_node_has_valid_defaults():
    registry = build_default_registry()
    assert len(registry.definitions()) >= 13
    for definition in registry.definitions():
        assert validate_config(definition, {}) == [], f"{definition.type} defaults must validate"


def test_config_validation_reports_real_problems():
    registry = build_default_registry()
    indicator = registry.get("finance.indicator")
    assert indicator is not None
    problems = validate_config(indicator, {"kind": "not-a-kind", "period": 9_999})
    assert any("kind" in p for p in problems)
    assert any("period" in p for p in problems)
    assert validate_config(indicator, {"kind": "sma", "period": 20}) == []


def test_every_registered_node_is_executable():
    """No node is left as a declaration-only stub; effects are gated by grants."""
    registry = build_default_registry()
    for definition in registry.definitions():
        assert definition.available, f"{definition.type} has no executor"
    video = registry.get("video.render")
    assert video is not None and video.executor is not None
    assert video.side_effects.value == "write"
    # The HTTP node is a real network effect; the agent node is a MODEL effect
    # (a test run may call it with a narrowed read-only surface).
    http = registry.get("integration.http")
    assert http is not None and http.executor is not None
    assert http.side_effects.value == "network"
    agent = registry.get("agent.research")
    assert agent is not None and agent.executor is not None
    assert agent.side_effects.value == "model"
    # The finance/quant pack gained real executors in Phase 5.
    for node_type in ("finance.market_data", "finance.indicator", "quant.returns", "quant.zscore", "quant.volatility"):
        definition = registry.get(node_type)
        assert definition is not None and definition.available, node_type


# ── Formula language ────────────────────────────────────────────────────────


def test_formula_evaluates_arithmetic_and_functions():
    assert evaluate_formula("x * 2 + 1", {"x": 3}) == 7
    assert evaluate_formula("(x + y) / 2", {"x": 1, "y": 4}) == 2.5
    assert evaluate_formula("2 ** 3", {}) == 8
    assert evaluate_formula("min(x, 5) + abs(-2)", {"x": 9}) == 7
    assert evaluate_formula("-x", {"x": 2}) == -2
    assert evaluate_formula("pi", {}) == pytest.approx(3.141592653589793)


def test_formula_refuses_unsafe_or_invalid_input():
    for expression in ("x / 0", "unknownfn(1)", "z + 1", "1 +", "x ** 100000", ""):
        with pytest.raises(FormulaError):
            evaluate_formula(expression, {"x": 2}, )
    with pytest.raises(FormulaError):
        evaluate_formula("__import__('os')", {})
    with pytest.raises(FormulaError):
        evaluate_formula("x" * 500, {})


def test_template_renders_and_refuses_unknown_names():
    assert render_template("{{a}}-{{b}}", {"a": "x", "b": 2}) == "x-2"
    assert render_template("{{ a }}", {"a": {"z": 1}}) == '{"z": 1}'
    with pytest.raises(ValueError):
        render_template("{{missing}}", {"a": 1})


def test_a_ticket_port_accepts_a_number_or_an_opaque_string():
    from harness.graph_runtime.model import PortKind
    from harness.graph_runtime.registry import PortDef, ports_compatible

    def port(kind: PortType) -> PortDef:
        return PortDef(id="p", name="P", type=kind, kind=PortKind.DATA)

    assert ports_compatible(port(PortType.NUMBER), port(PortType.TICKET))
    assert ports_compatible(port(PortType.STRING), port(PortType.TICKET))
    assert ports_compatible(port(PortType.TICKET), port(PortType.TICKET))
    assert not ports_compatible(port(PortType.BOOLEAN), port(PortType.TICKET))
    assert not ports_compatible(port(PortType.NUMBER), port(PortType.STRING))


def test_a_ticket_value_is_an_id_not_a_number_only():
    assert validate_port_value(PortType.TICKET, 314159) is None
    assert validate_port_value(PortType.TICKET, "b9a1e5c4-7f00") is None
    assert validate_port_value(PortType.TICKET, "BTC-USD") is None
    assert validate_port_value(PortType.TICKET, True)
    assert validate_port_value(PortType.TICKET, "")
    assert validate_port_value(PortType.TICKET, {"id": 1})
