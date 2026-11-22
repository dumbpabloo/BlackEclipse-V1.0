"""Composition: layers, patches, expressions and diagnostics."""

from __future__ import annotations

import pytest

from harness.kernel.composition import Composition, Layer, Row, compose
from harness.kernel.errors import CompositionError


def _layer(source: str, patch: list[dict]) -> Layer:
    return Layer(source=source, patch=patch)


def test_insert_then_override_replaces_the_whole_config():
    composition = compose([
        _layer("base", [{"insert": [
            {"id": "llm", "name": "pkg.llm:apply", "config": {"model": "a", "timeout": 5}},
        ]}]),
        _layer("user", [{"id": "llm", "config": {"model": "b"}}]),
    ])
    row = composition.by_id("llm")
    assert row is not None
    assert row.config == {"model": "b"}
    assert row.source == "user"


def test_disable_and_remove():
    composition = compose([
        _layer("base", [{"insert": [
            {"id": "one", "name": "pkg.one:apply"},
            {"id": "two", "name": "pkg.two:apply"},
        ]}]),
        _layer("user", [{"id": "one", "disabled": True}, {"remove": "two"}]),
    ])
    assert composition.by_id("one").disabled is True
    assert composition.by_id("two") is None
    assert [row.id for row in composition.enabled()] == []


def test_unknown_patch_target_is_a_diagnostic_not_a_crash():
    composition = compose([_layer("user", [{"id": "missing", "disabled": True}])])
    assert composition.rows == []
    assert composition.diagnostics[0]["code"] == "KERNEL_COMPOSITION_UNKNOWN_ROW"


def test_duplicate_insert_is_rejected():
    with pytest.raises(CompositionError) as info:
        compose([_layer("base", [{"insert": [
            {"id": "same", "name": "a:apply"},
            {"id": "same", "name": "b:apply"},
        ]}])])
    assert info.value.code == "KERNEL_COMPOSITION_DUPLICATE"


def test_unknown_row_key_is_rejected():
    with pytest.raises(CompositionError) as info:
        compose([_layer("base", [{"insert": [{"id": "x", "name": "a:apply", "typo": 1}]}])])
    assert info.value.code == "KERNEL_COMPOSITION_KEY"


def test_expressions_are_declarative_and_never_executed():
    composition = compose([
        _layer("base", [{"insert": [{
            "id": "row",
            "name": "a:apply",
            "disabled": {"$eq": [{"$env": "MODE"}, "off"]},
            "config": {
                "model": {"$env": "MODEL", "$default": "fallback"},
                "flag": {"$not": {"$env": "FLAG"}},
                "nested": {"keep": {"$literal": {"$env": "NOT_EVALUATED"}}},
            },
        }]}]),
    ], env={"MODE": "off", "FLAG": "false"})
    row = composition.by_id("row")
    assert row.disabled is True
    assert row.config["model"] == "fallback"
    assert row.config["flag"] is True
    assert row.config["nested"]["keep"] == {"$env": "NOT_EVALUATED"}


def test_ref_expression_reads_declared_settings_only():
    composition = compose([
        _layer("base", [{"insert": [{
            "id": "row", "name": "a:apply",
            "config": {"value": {"$ref": "settings.value", "$default": "x"}},
        }]}]),
    ], refs={"settings": {"value": "y"}})
    assert composition.by_id("row").config["value"] == "y"


def test_unknown_expression_key_is_rejected():
    with pytest.raises(CompositionError) as info:
        compose([_layer("base", [{"insert": [
            {"id": "row", "name": "a:apply", "config": {"x": {"$exec": "rm -rf /"}}},
        ]}])])
    assert info.value.code == "KERNEL_COMPOSITION_EXPR"


def test_row_identity_is_stable_for_diffing():
    row = Row(id="x", name="a:apply", config={"b": 1, "a": 2})
    assert row.to_dict()["config"] == {"b": 1, "a": 2}
    assert isinstance(Composition(rows=[row]).to_dict()["rows"], list)
