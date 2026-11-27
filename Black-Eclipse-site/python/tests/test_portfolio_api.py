"""Portfolio HTTP routes — operator-gated surface over the real engine."""

from __future__ import annotations

import os
import tempfile

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

os.environ.setdefault(
    "AGENT_MEMORY_DB_PATH",
    os.path.join(tempfile.mkdtemp(prefix="be-pf-api-"), "agent-memory.db"),
)


@pytest.fixture()
def client(tmp_path, monkeypatch):
    import harness.routers.portfolio as portfolio_router
    from portfolio_helpers import make_service

    class _StubService:
        pass

    stub = _StubService()
    stub.portfolio = make_service(tmp_path)
    monkeypatch.setattr(portfolio_router, "service", stub, raising=False)

    app = FastAPI()
    app.include_router(portfolio_router.router)
    return TestClient(app)


def test_create_is_a_fund_and_update_patches_metadata(client):
    created = client.post("/portfolio/create", json={
        "name": "Fund", "base_currency": "USD", "kind": "portfolio",
        "metadata": {"icon": "Rocket", "goal": 100},
    })
    assert created.status_code == 200, created.text
    record = created.json()
    assert record["kind"] == "fund"

    patched = client.patch(f"/portfolio/{record['id']}", json={
        "name": "Renamed", "metadata": {"goal": 250},
    })
    assert patched.status_code == 200, patched.text
    body = patched.json()
    assert body["name"] == "Renamed"
    assert body["kind"] == "fund"


def test_create_add_query_roundtrip(client):
    created = client.post("/portfolio/create", json={"name": "API", "base_currency": "USD"})
    assert created.status_code == 200
    pid = created.json()["id"]

    add = client.post(f"/portfolio/{pid}/position",
                      json={"symbol": "AAPL", "quantity": "10", "price": "100"})
    assert add.status_code == 200
    assert add.json()["status"] == "SUCCESS"

    query = client.post(f"/portfolio/{pid}/query",
                        json={"marks": {"AAPL": {"price": "110", "timestamp": "2026-10-08T00:00:00Z"}},
                              "include": ["positions", "pnl"]})
    assert query.status_code == 200
    body = query.json()
    assert body["positions"][0]["symbol"] == "AAPL"
    assert body["portfolio"]["costBasisMethod"] == "FIFO"


def test_get_unknown_portfolio_is_404(client):
    resp = client.get("/portfolio/does-not-exist")
    assert resp.status_code == 404


def test_rebalance_route_returns_a_proposal(client):
    pid = client.post("/portfolio/create", json={"name": "R"}).json()["id"]
    client.post(f"/portfolio/{pid}/position", json={"symbol": "AAPL", "quantity": "10", "price": "100"})
    resp = client.post(f"/portfolio/{pid}/rebalance",
                       json={"target": {"AAPL": 0.5, "CASH": 0.5},
                             "marks": {"AAPL": {"price": "120", "timestamp": "2026-10-08T00:00:00Z"}}})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "PROPOSED"
    assert "proposal-only" in body["execution"]


def test_tax_lots_route(client):
    pid = client.post("/portfolio/create", json={"name": "T"}).json()["id"]
    client.post(f"/portfolio/{pid}/position", json={"symbol": "AAPL", "quantity": "10", "price": "100"})
    client.post(f"/portfolio/{pid}/transaction",
                json={"type": "SELL", "symbol": "AAPL", "quantity": "4", "price": "150"})
    resp = client.get(f"/portfolio/{pid}/tax-lots")
    assert resp.status_code == 200
    body = resp.json()
    assert body["symbols"][0]["symbol"] == "AAPL"
    # The Funds UI's Tax Lots & Realized Gains widget reads these fields; keep
    # the contract pinned here rather than only in the TypeScript types.
    assert "totalRealizedPnl" in body
    assert body["symbols"][0]["matches"], "a disposal must produce a realized lot match"
    match = body["symbols"][0]["matches"][0]
    for key in ("proceeds", "costBasis", "realizedPnl", "acquiredAt", "disposedAt", "holdingDays"):
        assert key in match, f"missing {key} in lot match"
    assert match["holdingDays"] is not None
    # Currency travels with the lot so the UI can label native amounts.
    assert body["symbols"][0]["openLots"][0]["currency"] == "USD"


def test_update_persists_cost_basis_method(client):
    pid = client.post("/portfolio/create", json={"name": "HIFO fund"}).json()["id"]
    patched = client.patch(f"/portfolio/{pid}", json={"cost_basis_method": "HIFO"})
    assert patched.status_code == 200, patched.text
    # Re-read the query surface: the method must be the engine's persisted one,
    # not a display-only toggle.
    read = client.post(f"/portfolio/{pid}/query", json={"include": ["positions"]})
    assert read.status_code == 200
    assert read.json()["portfolio"]["costBasisMethod"] == "HIFO"


def test_account_routes_and_account_scoped_cash(client):
    pid = client.post("/portfolio/create", json={"name": "Accounts"}).json()["id"]

    created = client.post(f"/portfolio/{pid}/accounts", json={
        "name": "Broker A", "kind": "brokerage", "external_id": "EXT-1",
    })
    assert created.status_code == 200, created.text
    account = created.json()
    assert account["id"].startswith("acct_")

    listed = client.get(f"/portfolio/{pid}/accounts")
    assert [row["id"] for row in listed.json()["accounts"]] == [account["id"]]

    updated = client.patch(f"/portfolio/{pid}/accounts/{account['id']}",
                           json={"name": "Broker Prime"})
    assert updated.status_code == 200
    assert updated.json()["name"] == "Broker Prime"

    client.post(f"/portfolio/{pid}/transaction", json={
        "type": "DEPOSIT", "symbol": "CASH", "gross_amount": "1000",
        "account_id": account["id"],
    })
    client.post(f"/portfolio/{pid}/position", json={
        "symbol": "AAPL", "quantity": "2", "price": "100",
        "account_id": account["id"],
    })

    query = client.post(f"/portfolio/{pid}/query", json={"include": ["positions", "cash"]})
    assert query.status_code == 200, query.text
    body = query.json()
    row = next(item for item in body["accounts"] if item["id"] == account["id"])
    assert float(row["cash"]) == 800.0
    assert float(row["positions"][0]["quantity"]) == 2.0
    assert body["accountsReconcile"] is True

    lots = client.get(f"/portfolio/{pid}/tax-lots",
                      params={"account_id": account["id"]})
    assert lots.status_code == 200
    assert lots.json()["accountId"] == account["id"]

    in_use = client.delete(f"/portfolio/{pid}/accounts/{account['id']}")
    assert in_use.status_code == 409


def test_audit_log_and_verify_routes(client):
    pid = client.post("/portfolio/create", json={"name": "Audit"}).json()["id"]
    client.post(f"/portfolio/{pid}/position", json={"symbol": "AAPL", "quantity": "1", "price": "100"})

    trail = client.get(f"/portfolio/{pid}/audit")
    assert trail.status_code == 200
    events = trail.json()["events"]
    assert events and events[0]["hash"]
    assert events[0]["prevHash"]

    verified = client.get(f"/portfolio/{pid}/audit/verify")
    assert verified.status_code == 200
    body = verified.json()
    assert body["ok"] is True, body
    assert body["status"] == "OK"
    assert body["checked"] == len(events)


def test_realized_gains_route(client):
    pid = client.post("/portfolio/create", json={"name": "Tax"}).json()["id"]
    client.post(f"/portfolio/{pid}/transaction", json={
        "type": "BUY", "symbol": "AAPL", "quantity": "10", "price": "100",
        "trade_date": "2025-01-01",
    })
    client.post(f"/portfolio/{pid}/transaction", json={
        "type": "SELL", "symbol": "AAPL", "quantity": "10", "price": "150",
        "trade_date": "2026-02-01",
    })

    resp = client.get(f"/portfolio/{pid}/realized-gains")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["method"] == "FIFO"
    assert body["years"][0]["taxYear"] == "2026"
    assert body["years"][0]["currency"] == "USD"
    assert len(body["disposals"]) == 1
    assert body["washSale"]["applied"] is False


def test_corporate_action_route(client):
    pid = client.post("/portfolio/create", json={"name": "CA"}).json()["id"]
    client.post(f"/portfolio/{pid}/position", json={"symbol": "X", "quantity": "10", "price": "100"})
    resp = client.post(f"/portfolio/{pid}/corporate-action", json={"action": "SPLIT", "symbol": "X", "ratio": "2"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "SUCCESS"


def test_swap_route(client):
    pid = client.post("/portfolio/create", json={"name": "SW"}).json()["id"]
    client.post(f"/portfolio/{pid}/position", json={"symbol": "BTC", "quantity": "1", "price": "30000"})
    resp = client.post(f"/portfolio/{pid}/swap",
                       json={"from_symbol": "BTC", "to_symbol": "ETH", "from_quantity": "1",
                             "from_price": "40000", "to_price": "2000"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "SUCCESS"


def test_reconcile_route(client):
    pid = client.post("/portfolio/create", json={"name": "RC"}).json()["id"]
    client.post(f"/portfolio/{pid}/position", json={"symbol": "AAPL", "quantity": "10", "price": "100"})
    resp = client.post(f"/portfolio/{pid}/reconcile",
                       json={"external_positions": [{"symbol": "AAPL", "quantity": "10"}]})
    assert resp.status_code == 200
    assert resp.json()["status"] == "MATCHED"


def test_optimize_route(client):
    pid = client.post("/portfolio/create", json={"name": "OP"}).json()["id"]
    resp = client.post(f"/portfolio/{pid}/optimize",
                       json={"symbols": ["A", "B"], "covariance": [[0.04, 0], [0, 0.01]],
                             "method": "min_variance"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["available"] is True
    assert body["fullyInvested"] is True


def test_aggregate_route(client):
    p1 = client.post("/portfolio/create", json={"name": "A1"}).json()["id"]
    p2 = client.post("/portfolio/create", json={"name": "A2"}).json()["id"]
    client.post(f"/portfolio/{p1}/position", json={"symbol": "AAPL", "quantity": "10", "price": "100"})
    client.post(f"/portfolio/{p2}/position", json={"symbol": "MSFT", "quantity": "5", "price": "200"})
    resp = client.post("/portfolio/aggregate", json={"portfolio_ids": [p1, p2]})
    assert resp.status_code == 200
    assert resp.json()["portfolioCount"] == 2


def test_tenant_isolation_over_http(client):
    created = client.post("/portfolio/create", json={"name": "Alice"},
                          headers={"X-Harness-User": "alice"})
    pid = created.json()["id"]
    assert created.json()["ownerId"] == "alice"
    # Alice can read it
    assert client.get(f"/portfolio/{pid}", headers={"X-Harness-User": "alice"}).status_code == 200
    # Bob cannot — a changed id is not enough
    denied = client.get(f"/portfolio/{pid}", headers={"X-Harness-User": "bob"})
    assert denied.status_code == 403
