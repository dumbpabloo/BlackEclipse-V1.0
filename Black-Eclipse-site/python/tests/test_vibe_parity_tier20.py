"""Branch parity, twentieth tier: OpenBB workspace context is INTENT, not data.

Reference fetched 2026-11-21 (MIT, `HKUDS/Vibe-Trading`,
`agent/src/openbb_bridge/context_injector.py`).

Eclipse injected the request's attached **context items** and nothing else. The
reference also renders the user's **widgets and dashboard** — and, crucially, says in
the block that their **values are not attached**. That sentence is the whole point:
OpenBB does not put widget values in a `/v1/query` request (fetching them needs a
`get_widget_data` round trip this bridge does not implement), so a model shown
"widget: AAPL price" will otherwise answer from memory instead of fetching. Eclipse
would have shipped exactly that gap.

The budget constants are the reference's, and they are numeric, so they are pinned:

    MAX_WIDGETS = 10   MAX_VALUE_CHARS = 200   MAX_PARAMS_PER_WIDGET = 8
"""

from __future__ import annotations

import pytest

from harness.openbb_bridge.adapter import (
    MAX_PARAMS_PER_WIDGET,
    MAX_WIDGETS,
    MAX_WIDGET_VALUE_CHARS,
    WIDGET_VALUES_NOT_ATTACHED,
    prepare,
)


def _request(widgets=None, *, text="what is AAPL doing", context=None):
    payload = {"messages": [{"role": "user", "content": text}]}
    if widgets is not None:
        payload["widgets"] = widgets
    if context is not None:
        payload["context"] = context
    return payload


# -- constants ------------------------------------------------------------

def test_the_widget_budget_constants_are_the_references():
    assert MAX_WIDGETS == 10
    assert MAX_WIDGET_VALUE_CHARS == 200
    assert MAX_PARAMS_PER_WIDGET == 8


# -- the honesty line -----------------------------------------------------

def test_widget_values_are_declared_as_not_attached():
    message = prepare(_request([{"name": "Price", "params": [
        {"name": "symbol", "current_value": "AAPL"}]}])).user_message
    assert WIDGET_VALUES_NOT_ATTACHED in message
    assert "NOT attached" in message
    assert "instead of guessing" in message


def test_the_block_is_fenced_and_carries_the_parameters_as_intent():
    message = prepare(_request([{
        "name": "Price", "origin": "openbb", "description": "AAPL price chart",
        "params": [{"name": "symbol", "current_value": "AAPL"},
                   {"name": "interval"}],
    }])).user_message
    assert "<workspace-widgets>" in message and "</workspace-widgets>" in message
    assert "- Price (source: openbb): AAPL price chart" in message
    assert "params: symbol=AAPL, interval" in message


# -- the numeric limits ---------------------------------------------------

def test_more_than_ten_widgets_are_capped_and_the_rest_counted():
    widgets = [{"name": f"W{i}"} for i in range(13)]
    message = prepare(_request(widgets)).user_message
    assert message.count("- W") == 10
    assert "...and 3 more widget(s)." in message


def test_a_long_widget_value_is_clipped_with_an_ellipsis():
    long_value = "x" * 500
    message = prepare(_request([{
        "name": "W", "params": [{"name": "ticker", "current_value": long_value}],
    }])).user_message
    assert "x" * MAX_WIDGET_VALUE_CHARS not in message
    assert "..." in message


def test_only_eight_parameters_per_widget_are_rendered():
    params = [{"name": f"p{i}", "current_value": i} for i in range(12)]
    message = prepare(_request([{"name": "W", "params": params}])).user_message
    assert "p7=" in message
    assert "p8=" not in message


# -- absence and tolerance ------------------------------------------------

def test_no_widgets_means_no_block_and_the_message_is_unchanged():
    prepared = prepare(_request(text="plain question"))
    assert prepared.user_message == "plain question"
    assert "<workspace-widgets>" not in prepared.user_message


def test_a_widget_with_no_name_is_labelled_rather_than_dropped():
    message = prepare(_request([{"params": [{"name": "symbol"}]}])).user_message
    assert "Unnamed widget" in message


def test_junk_in_the_widgets_array_never_breaks_the_turn():
    message = prepare(_request([None, 42, {"name": "Real"}])).user_message
    assert "- Real" in message
    assert "42" not in message.split("<workspace-widgets>")[1].split("</workspace-widgets>")[0]


def test_attached_context_still_comes_through_alongside_widgets():
    message = prepare(_request(
        [{"name": "Price"}],
        context=[{"name": "filings", "description": "10-K extracts"}],
    )).user_message
    assert "<workspace-context>" in message
    assert "<workspace-widgets>" in message
    assert message.index("</workspace-context>") < message.index("<workspace-widgets>")


def test_the_first_user_turn_must_be_a_user_turn_still():
    with pytest.raises(Exception):
        prepare({"messages": [{"role": "assistant", "content": "hi"}], "widgets": []})
