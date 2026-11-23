"""The registry's execution flag must match the adapter it points at.

A kind that claims `execution: true` but returns a read-only adapter is worse
than a read-only kind: the UI offers an order control and the submit path fails
with BROKER_READ_ONLY. The reverse (an executing adapter declared read-only)
hides a working path. This test fails on either drift.
"""

from __future__ import annotations

from harness.trade.adapter import TradingAdapter
from harness.trade.registry import list_kinds, make_adapter


def test_every_kind_declares_the_execution_it_can_actually_do():
    mismatches: list[str] = []
    for kind in list_kinds():
        adapter = make_adapter(kind["id"], {})
        can_execute = isinstance(adapter, TradingAdapter)
        declared = bool(kind["execution"])
        if can_execute != declared:
            mismatches.append(
                f"{kind['id']}: adapter execution={can_execute}, declared={declared}"
            )
    assert not mismatches, "; ".join(mismatches)


def test_an_executing_kind_names_its_modes():
    for kind in list_kinds():
        if kind["execution"]:
            assert kind["executionModes"], f"{kind['id']} executes but names no mode"
            for mode in kind["executionModes"]:
                assert mode in kind["modes"], f"{kind['id']} executes a mode it does not declare"


def test_venue_selector_config_keys_are_storable():
    """A venue setting the adapter reads must survive validate_config."""
    from harness.trade.model import validate_config

    config = validate_config({
        "quoteAsset": "USDT",
        "category": "linear",
        "productType": "USDT-FUTURES",
        "settle": "usdt",
        "instType": "SWAP",
        "tdMode": "isolated",
        "marginMode": "cross",
        "market": "spot",
        "authStyle": "key",
        "currency": "USD",
    })
    assert config["quoteAsset"] == "USDT"
    assert config["category"] == "linear"
    assert config["tdMode"] == "isolated"
    assert config["authStyle"] == "key"
