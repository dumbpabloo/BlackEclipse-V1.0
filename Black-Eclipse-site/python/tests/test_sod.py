"""Segregation-of-duties policies: off by default, composed above single-use approvals."""

from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from harness.ledger.authorizers import LEDGER_APPROVAL_KIND, make_ledger_authorizer
from harness.mcp.approvals import ApprovalStore
from harness.persistence import AgentMemoryStore
from harness.portfolio.authorizers import make_portfolio_authorizer
from harness.sod import SoDError, SoDService


def _stack(tmp_path):
    store = AgentMemoryStore(os.path.join(str(tmp_path), "sod-test.db"))
    approvals = ApprovalStore(store)
    sod = SoDService(store)
    approvals.sod = sod
    return approvals, sod


def _ledger_tool():
    return SimpleNamespace(name="ledger_post", effect="ledger.write")


def _ledger_args(amount: str = "500"):
    return {
        "ledger_id": "L1",
        "lines": [
            {"account_code": "1010", "debit": amount},
            {"account_code": "3000", "credit": amount},
        ],
    }


def test_no_policy_keeps_the_single_use_flow_unchanged(tmp_path):
    approvals, sod = _stack(tmp_path)
    ctx = SimpleNamespace(user_id="alice")
    authorize = make_ledger_authorizer(approvals, "run-1", "ws")

    assert sod.check("ledger.write", "500", "digest", requester="alice")["satisfied"] is True

    assert asyncio.run(
        authorize(_ledger_tool(), _ledger_args(), ctx)
    ) is False
    pending = approvals.list_kind(LEDGER_APPROVAL_KIND, status="pending", limit=10)
    assert len(pending) == 1
    approvals.decide(pending[0]["id"], approve=True, decided_by="operator")

    assert asyncio.run(
        authorize(_ledger_tool(), _ledger_args(), ctx)
    ) is True


def test_policy_requires_distinct_approvers_in_addition_to_single_use(tmp_path):
    approvals, sod = _stack(tmp_path)
    ctx = SimpleNamespace(user_id="alice")
    authorize = make_ledger_authorizer(approvals, "run-1", "ws")
    sod.set_policy("ledger.write", threshold_amount="100", required_approvals=2,
                   separate_requester=True, actor="admin")

    assert sod.check("ledger.write", "50", "digest")["satisfied"] is True
    blocked = sod.check("ledger.write", "500", "digest", requester="alice")
    assert blocked["satisfied"] is False
    assert blocked["required"] == 2

    # The authorizer refuses and leaves a pending approval, without consuming it.
    assert asyncio.run(
        authorize(_ledger_tool(), _ledger_args(), ctx)
    ) is False
    pending = approvals.list_kind(LEDGER_APPROVAL_KIND, status="pending", limit=10)
    assert len(pending) == 1

    digest = pending[0]["argsDigest"]
    first = sod.sign_off("ledger.write", digest, "bob", amount="500")
    assert first["satisfied"] is False and first["remaining"] == 1

    # Duplicate approver does not count twice.
    again = sod.sign_off("ledger.write", digest, "bob", amount="500")
    assert again["satisfied"] is False and again["collected"] == ["bob"]

    # Approving the single-use card alone is still not enough.
    approvals.decide(pending[0]["id"], approve=True, decided_by="operator")
    assert asyncio.run(
        authorize(_ledger_tool(), _ledger_args(), ctx)
    ) is False

    second = sod.sign_off("ledger.write", digest, "carol", amount="500")
    assert second["satisfied"] is True and second["collected"] == ["bob", "carol"]

    assert asyncio.run(
        authorize(_ledger_tool(), _ledger_args(), ctx)
    ) is True
    # Both gates are spent: a second identical call needs a new approval + sign-offs.
    assert sod.list_signoffs("ledger.write", digest) == []
    assert asyncio.run(
        authorize(_ledger_tool(), _ledger_args(), ctx)
    ) is False


def test_requester_cannot_satisfy_their_own_policy(tmp_path):
    _, sod = _stack(tmp_path)
    sod.set_policy("ledger.write", threshold_amount="0", required_approvals=2,
                   separate_requester=True)
    with pytest.raises(SoDError) as info:
        sod.sign_off("ledger.write", "d", "alice", amount="10", requester="alice")
    assert info.value.code == "SOD_SELF_APPROVAL"


def test_highest_applicable_threshold_wins_and_can_be_deactivated(tmp_path):
    _, sod = _stack(tmp_path)
    low = sod.set_policy("ledger.write", threshold_amount="100", required_approvals=2)
    high = sod.set_policy("ledger.write", threshold_amount="1000", required_approvals=3)

    assert sod.check("ledger.write", "500", "d")["required"] == 2
    assert sod.check("ledger.write", "2000", "d")["required"] == 3
    assert sod.deactivate_policy(high["id"]) is True
    assert sod.check("ledger.write", "2000", "d")["required"] == 2
    assert low["id"] in [row["id"] for row in sod.list_policies("ledger.write")]


def test_portfolio_authorizer_reads_the_same_policy(tmp_path):
    approvals, sod = _stack(tmp_path)
    sod.set_policy("portfolio.write", threshold_amount="100", required_approvals=2)
    ctx = SimpleNamespace(user_id="pm")
    authorize = make_portfolio_authorizer(approvals, "run-2", "ws")
    tool = SimpleNamespace(name="portfolio_add_position", effect="portfolio.write")
    args = {"portfolio_id": "P1", "symbol": "AAPL", "quantity": "10", "price": "50"}

    assert asyncio.run(authorize(tool, args, ctx)) is False
    pending = approvals.list_kind("portfolio_write", status="pending", limit=10)
    assert pending
    digest = pending[0]["argsDigest"]
    sod.sign_off("portfolio.write", digest, "a", amount="500")
    sod.sign_off("portfolio.write", digest, "b", amount="500")
    approvals.decide(pending[0]["id"], approve=True, decided_by="operator")
    assert asyncio.run(authorize(tool, args, ctx)) is True


def test_sod_routes_are_operator_surface(tmp_path):
    import harness.routers.sod as sod_router

    approvals, sod = _stack(tmp_path)

    class _Stub:
        pass

    stub = _Stub()
    stub.sod = sod
    sod_router.service = stub

    app = FastAPI()
    app.include_router(sod_router.router)
    client = TestClient(app)

    created = client.post("/sod/policies", json={
        "effect": "ledger.write", "threshold_amount": "100",
        "required_approvals": 2, "separate_requester": True,
    })
    assert created.status_code == 200, created.text
    policy = created.json()
    assert policy["requiredApprovals"] == 2

    listed = client.get("/sod/policies", params={"effect": "ledger.write"})
    assert [row["id"] for row in listed.json()["policies"]] == [policy["id"]]

    status = client.get("/sod/status", params={
        "effect": "ledger.write", "digest": "d1", "amount": "500", "requester": "alice",
    })
    assert status.status_code == 200
    assert status.json()["required"] == 2 and status.json()["satisfied"] is False

    signed = client.post("/sod/signoffs", json={
        "effect": "ledger.write", "digest": "d1", "approver": "bob", "amount": "500",
    })
    assert signed.status_code == 200
    assert signed.json()["remaining"] == 1

    self_approve = client.post("/sod/signoffs", json={
        "effect": "ledger.write", "digest": "d2", "approver": "alice",
        "amount": "500", "requester": "alice",
    })
    assert self_approve.status_code == 409
