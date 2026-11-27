"""An order must be traceable to the bot and the objective version that placed it.

P4: without this, "what has this bot actually done?" needs a two-table join and a
version bump is invisible on the orders it produced. The advisory ledger already
carries `runId` + `conversationId`, so the recommendation behind an order is
found by run id — no new write path is needed for that half.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from types import SimpleNamespace

from harness.trade.authorizer import approval_view
from harness.trade.store import BrokerConnectionStore
from harness.trade.tools import _bot_from_conversation, order_ledger
from harness.trade.authority import bot_version as _bot_version
from harness.trade.bootstrap import set_service


class _Db:
    def __init__(self, db: sqlite3.Connection) -> None:
        self._db = db


def _store() -> BrokerConnectionStore:
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    return BrokerConnectionStore(_Db(db))


_INTENT = {"symbol": "EURUSD", "side": "buy", "type": "market", "volume": 0.1}


def test_an_order_records_the_bot_and_its_version():
    store = _store()
    order = store.create_order(
        connection_id="brk_1", intent=_INTENT, digest="d1",
        run_id="run_1", automation_id="auto_1", automation_version=7,
    )
    assert order["automationId"] == "auto_1"
    assert order["automationVersion"] == 7
    assert order["runId"] == "run_1"


def test_the_ledger_can_be_filtered_to_one_bot():
    store = _store()
    store.create_order(connection_id="brk_1", intent=_INTENT, digest="d1",
                       automation_id="auto_1", automation_version=1)
    store.create_order(connection_id="brk_1", intent=_INTENT, digest="d2",
                       automation_id="auto_2", automation_version=1)
    store.create_order(connection_id="brk_1", intent=_INTENT, digest="d3")

    mine = store.list_orders(automation_id="auto_1")
    assert len(mine) == 1 and mine[0]["automationId"] == "auto_1"
    assert len(store.list_orders()) == 3


def test_an_order_without_a_bot_is_still_valid():
    store = _store()
    order = store.create_order(connection_id="brk_1", intent=_INTENT, digest="d1")
    assert order["automationId"] is None
    assert order["automationVersion"] is None


def test_a_bot_conversation_yields_its_automation_id():
    assert _bot_from_conversation(SimpleNamespace(conversation_id="bot_1")) == ""
    assert _bot_from_conversation(SimpleNamespace(conversation_id="bot-auto_1")) == "auto_1"
    assert _bot_from_conversation(SimpleNamespace(conversation_id=None)) == ""
    assert _bot_from_conversation(SimpleNamespace()) == ""


def test_an_approval_remembers_the_bot_that_proposed_it():
    view = approval_view({
        "id": "appr_1",
        "kind": "trade",
        "status": "pending",
        "serverId": "trade:brk_1",
        "affectedData": json.dumps({
            "automationId": "auto_1",
            "order": {"symbol": "EURUSD"},
        }),
    })
    assert view["automationId"] == "auto_1"
    assert view["connectionId"] == "brk_1"


def test_the_version_lookup_is_best_effort():
    assert _bot_version("") is None
    # No automation service installed: unknown, never an exception.
    set_service(None)
    assert _bot_version("auto_1") is None


def test_order_ledger_names_the_bot_and_version_in_its_text():
    store = _store()
    order = store.create_order(
        connection_id="brk_1", intent=_INTENT, digest="d1",
        run_id="run_1", automation_id="auto_1", automation_version=4,
    )
    service = SimpleNamespace(
        ledger_orders=lambda connection_id, limit=20, automation_id=None: [order]
    )
    ctx = SimpleNamespace(conversation_id="bot-auto_1", brokers=service)
    text, payload = asyncio.run(order_ledger({}, ctx))
    assert "bot auto_1@v4" in text
    assert payload["orders"][0]["automationId"] == "auto_1"
