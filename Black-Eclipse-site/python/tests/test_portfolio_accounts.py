"""Per-account cash and lot separation on the existing PortfolioAccount table."""

from __future__ import annotations

import pytest

from harness.portfolio.errors import PortfolioError
from portfolio_helpers import make_portfolio, make_service


def _deposit(service, pid: str, account_id: str | None, amount: str) -> dict:
    payload = {"type": "DEPOSIT", "symbol": "CASH", "gross_amount": amount,
               "currency": "USD"}
    if account_id:
        payload["account_id"] = account_id
    return service.add_transaction(pid, payload)


def test_account_crud_and_external_id_uniqueness(tmp_path):
    service = make_service(tmp_path)
    pid = make_portfolio(service)

    account = service.create_account(pid, "Broker A", kind="brokerage",
                                     institution="Acme", external_id="EXT-1")
    assert account["id"].startswith("acct_")
    assert account["currency"] == "USD"
    assert [row["id"] for row in service.list_accounts(pid)] == [account["id"]]

    with pytest.raises(PortfolioError) as info:
        service.create_account(pid, "Duplicate", external_id="EXT-1")
    assert info.value.code == "ACCOUNT_EXISTS"

    updated = service.update_account(pid, account["id"], {"name": "Broker Prime"})
    assert updated["name"] == "Broker Prime"

    deleted = service.delete_account(pid, account["id"])
    assert deleted["deleted"] is True
    assert service.list_accounts(pid) == []


def test_account_with_transactions_cannot_be_deleted(tmp_path):
    service = make_service(tmp_path)
    pid = make_portfolio(service)
    account = service.create_account(pid, "Cash")
    _deposit(service, pid, account["id"], "100")

    with pytest.raises(PortfolioError) as info:
        service.delete_account(pid, account["id"])
    assert info.value.code == "ACCOUNT_IN_USE"

    assert service.add_transaction(pid, {
        "type": "BUY", "symbol": "AAPL", "quantity": "1", "price": "10",
        "account_id": "acct_missing",
    })["status"] == "FAILED"


def test_read_reports_cash_and_positions_per_account_and_reconciles(tmp_path):
    service = make_service(tmp_path)
    pid = make_portfolio(service)
    a = service.create_account(pid, "Broker A")
    b = service.create_account(pid, "Broker B")

    _deposit(service, pid, a["id"], "10000")
    _deposit(service, pid, b["id"], "5000")
    service.add_transaction(pid, {
        "type": "FEE", "symbol": "CASH", "gross_amount": "-1000",
        "account_id": a["id"], "currency": "USD",
    })
    service.add_transaction(pid, {
        "type": "BUY", "symbol": "AAPL", "quantity": "10", "price": "100",
        "account_id": a["id"], "trade_date": "2026-01-02",
    })
    service.add_transaction(pid, {
        "type": "BUY", "symbol": "AAPL", "quantity": "5", "price": "100",
        "account_id": b["id"], "trade_date": "2026-01-03",
    })

    import asyncio

    report = asyncio.run(service.read(pid, include=["positions", "cash"]))
    accounts = {row["id"]: row for row in report["accounts"]}
    assert float(accounts[a["id"]]["cash"]) == 8000.0   # 10,000 − fee 1,000 − buy 1,000
    assert float(accounts[b["id"]]["cash"]) == 4500.0   # 5,000 − buy 500
    assert float(accounts[a["id"]]["positions"][0]["quantity"]) == 10.0
    assert float(accounts[b["id"]]["positions"][0]["quantity"]) == 5.0
    assert float(report["accountCashTotal"]) == float(report["cash"]["balance"])
    assert report["accountsReconcile"] is True
    # No transaction was left unassigned, so there is no synthetic bucket.
    assert all(not row["synthetic"] for row in report["accounts"])


def test_lots_and_oversell_are_account_scoped(tmp_path):
    service = make_service(tmp_path)
    pid = make_portfolio(service)
    a = service.create_account(pid, "A")
    b = service.create_account(pid, "B")
    service.add_transaction(pid, {"type": "BUY", "symbol": "AAPL", "quantity": "10",
                                  "price": "100", "account_id": a["id"]})
    service.add_transaction(pid, {"type": "BUY", "symbol": "AAPL", "quantity": "5",
                                  "price": "200", "account_id": b["id"]})

    # A holds 10, B holds 5. Selling 10 in B must be refused even though the
    # portfolio total is 15.
    refused = service.add_transaction(pid, {
        "type": "SELL", "symbol": "AAPL", "quantity": "10", "price": "150",
        "account_id": b["id"],
    })
    assert refused["status"] == "FAILED"
    assert refused["error"]["code"] == "INSUFFICIENT_POSITION"

    sold = service.add_transaction(pid, {
        "type": "SELL", "symbol": "AAPL", "quantity": "10", "price": "150",
        "account_id": a["id"],
    })
    assert sold["status"] == "SUCCESS"

    a_lots = service.calculate_tax_lots(pid, account_id=a["id"])
    b_lots = service.calculate_tax_lots(pid, account_id=b["id"])
    assert a_lots["accountId"] == a["id"]
    assert float(a_lots["symbols"][0]["remainingQuantity"]) == 0.0
    assert len(a_lots["symbols"][0]["matches"]) == 1
    assert float(b_lots["symbols"][0]["remainingQuantity"]) == 5.0
    assert float(b_lots["symbols"][0]["openLots"][0]["costPerUnit"]) == 200.0
