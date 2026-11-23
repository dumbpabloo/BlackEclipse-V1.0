"""The order-identity contract: MetaTrader ints and REST opaque ids coexist."""

from __future__ import annotations

import pytest

from harness.trade.model import TradeRefused, ticket_ref


def test_a_plain_decimal_id_is_an_int():
    assert ticket_ref(4242) == 4242
    assert ticket_ref("4242") == 4242
    assert ticket_ref(" 4242 ") == 4242


def test_an_opaque_id_keeps_its_exact_form():
    uuid = "b9a1e5c4-7f00-4a3e-9d1c-0e6a2b7f1234"
    assert ticket_ref(uuid) == uuid
    assert ticket_ref("ord_01HZX") == "ord_01HZX"
    assert ticket_ref("BTC-USD") == "BTC-USD"


def test_a_numeric_id_with_a_leading_zero_is_not_renumbered():
    # An exchange id is not a number: dropping the zero would address a
    # different order.
    assert ticket_ref("00123") == "00123"


def test_an_empty_or_boolean_id_is_refused():
    for value in (None, "", "   ", True):
        with pytest.raises(TradeRefused):
            ticket_ref(value)


def test_a_path_segment_is_not_a_ticket():
    for value in (".", ".."):
        with pytest.raises(TradeRefused):
            ticket_ref(value)
