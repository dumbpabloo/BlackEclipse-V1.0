"""An existing database's INTEGER-keyed position cache is rebuilt for text ids.

REST venues return opaque position ids. The snapshot table used to declare
`ticket INTEGER PRIMARY KEY`, which refuses a non-numeric id, so a one-time
rebuild preserves the cached rows and changes the key type. The broker remains
the source of truth, but an upgrade must not fail on an existing file.
"""

from __future__ import annotations

import sqlite3

from harness.trade.store import BrokerConnectionStore

_OLD_SCHEMA = """
CREATE TABLE BrokerPositionSnapshot (
  connectionId  TEXT NOT NULL,
  ticket        INTEGER NOT NULL,
  symbol        TEXT NOT NULL,
  side          TEXT NOT NULL,
  volume        REAL NOT NULL,
  openPrice     REAL NOT NULL,
  currentPrice  REAL NOT NULL,
  stopLoss      REAL,
  takeProfit    REAL,
  profit        REAL NOT NULL DEFAULT 0,
  swap          REAL NOT NULL DEFAULT 0,
  magic         INTEGER NOT NULL DEFAULT 0,
  comment       TEXT,
  openedAt      INTEGER NOT NULL,
  syncedAt      TEXT NOT NULL,
  PRIMARY KEY(connectionId, ticket)
);
"""


class _Store:
    def __init__(self, db):
        self._db = db


def _snapshot(ticket):
    return {
        "ticket": ticket, "symbol": "BTC", "side": "buy", "volume": 1.0,
        "openPrice": 10.0, "currentPrice": 11.0, "stopLoss": None,
        "takeProfit": None, "profit": 1.0, "swap": 0.0, "magic": 0,
        "comment": "test", "openedAt": 1,
    }


def test_an_integer_keyed_snapshot_table_is_rebuilt_and_preserved():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript(_OLD_SCHEMA)
    db.execute(
        "INSERT INTO BrokerPositionSnapshot VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("conn", 42, "EURUSD", "buy", 0.1, 1.1, 1.1, None, None, 0.0, 0.0, 0, "", 1, "now"),
    )
    store = BrokerConnectionStore(_Store(db))

    columns = {
        row["name"]: str(row["type"] or "").upper()
        for row in db.execute("PRAGMA table_info(BrokerPositionSnapshot)").fetchall()
    }
    assert columns["ticket"] == "TEXT"
    assert [row["ticket"] for row in store.list_positions("conn")] == ["42"]

    store.replace_positions("conn", [_snapshot("b9a1e5c4-7f00-4a3e-9d1c-0e6a2b7f1234")])
    assert store.list_positions("conn")[0]["ticket"] == "b9a1e5c4-7f00-4a3e-9d1c-0e6a2b7f1234"


def test_a_fresh_database_declares_a_text_key():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    store = BrokerConnectionStore(_Store(db))
    columns = {
        row["name"]: str(row["type"] or "").upper()
        for row in db.execute("PRAGMA table_info(BrokerPositionSnapshot)").fetchall()
    }
    assert columns["ticket"] == "TEXT"
    store.replace_positions("conn", [_snapshot("ord_01HZX")])
    assert store.list_positions("conn")[0]["ticket"] == "ord_01HZX"
