"""Auto-renew: the promise that a bot keeps its bounded authority alive.

The regression this guards is "autonomous trading silently stops": a live arm
died after eight hours and a mandate after a day, and nothing renewed them even
though auto mode says the bot renews its own mandate while its standing consent
is valid.

Coverage:
  * `renew_arm` extends a live arm that is still valid, never an expired one;
  * `authority_status` reports the real countdown from the stored rows;
  * `ensure_authority` replaces an exhausted/expired mandate, points the trial at
    it, revokes the old grant, and leaves a healthy mandate alone;
  * every renewal is on the connection audit.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from harness.mcp.approvals import ApprovalStore
from harness.persistence import AgentMemoryStore
from harness.trade import renewal
from harness.trade.model import BrokerKind, TradeRefused
from harness.trade.registry import register_kind
from harness.trade.service import BrokerService


class _NoopAdapter:
    """A registered kind needs a class; these tests never place an order."""

    kind = "renew-test"

    @classmethod
    def availability(cls):
        return True, None


def _service(tmp_path) -> BrokerService:
    store = AgentMemoryStore(os.path.join(str(tmp_path), "renew.db"))
    approvals = ApprovalStore(store)
    register_kind(
        BrokerKind(id="renew-test", label="Renew test", auth="attach",
                   execution=True, execution_modes=("demo", "live")),
        _NoopAdapter,
        replace=True,
    )
    return BrokerService(store, approvals=approvals, guardrails=None)


def _connection(service: BrokerService, *, mode: str = "demo") -> dict:
    return service.create_connection(
        kind="renew-test",
        label=f"Renew {mode}",
        mode=mode,
        config={"maxMargin": 500, "magic": 99},
    )


def _iso(seconds: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat().replace(
        "+00:00", "Z"
    )


class _FakeLive:
    """Only the two seams `ensure_authority` needs."""

    def __init__(self, trial: dict) -> None:
        self.trial = dict(trial)

    def get(self, _trial_id: str) -> dict:
        return dict(self.trial)

    def _update(self, _trial_id: str, **fields) -> dict:
        self.trial.update(fields)
        return dict(self.trial)


# â”€â”€ arm â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def test_an_expired_arm_is_never_resurrected(tmp_path):
    service = _service(tmp_path)
    connection = _connection(service, mode="live")

    expired = renewal.renew_arm(service, connection["id"])
    assert expired["renewed"] is False
    assert expired["reason"] == "ARM_EXPIRED"
    assert service.get_connection(connection["id"])["armedUntil"] is None


def test_a_valid_arm_is_extended_only_when_due(tmp_path):
    service = _service(tmp_path)
    connection = _connection(service, mode="live")
    service.connections.set_arm(connection["id"], until=_iso(600), by="operator")

    renewed = renewal.renew_arm(service, connection["id"])
    assert renewed["renewed"] is True
    assert renewed["ttlSeconds"] == renewal.MAX_ARM_TTL_SECONDS
    left = renewal._seconds_left(service.get_connection(connection["id"])["armedUntil"])
    assert left > 7 * 3600

    again = renewal.renew_arm(service, connection["id"])
    assert again["renewed"] is False
    assert again["reason"] == "not-due"

    events = [event["kind"] for event in service.connections.list_events(connection["id"])]
    assert "arm" in events


def test_a_demo_connection_has_no_arm_to_renew(tmp_path):
    service = _service(tmp_path)
    connection = _connection(service, mode="demo")
    assert renewal.renew_arm(service, connection["id"])["reason"] == "not-live"


# â”€â”€ status â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def test_authority_status_reads_the_stored_rows(tmp_path):
    service = _service(tmp_path)
    connection = _connection(service, mode="demo")
    label = connection["label"]
    mandate = service.create_mandate(
        connection["id"],
        {
            "mode": "demo",
            "symbols": ["EURUSD"],
            "maxOrders": 4,
            "maxMargin": 100,
            "ttlSeconds": 86400,
            "confirmation": f"MANDATE DEMO {label}",
        },
    )
    trial = {
        "mode": "demo",
        "connectionId": connection["id"],
        "mandateId": mandate["id"],
    }

    status = renewal.authority_status(service, None, trial)
    assert status["available"] is True
    assert status["maxOrders"] == 4
    assert status["ordersRemaining"] == 4
    assert status["needsMandateRenewal"] is False
    assert status["mandateSecondsLeft"] > 0


# â”€â”€ mandate renewal â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def test_a_healthy_mandate_is_left_alone(tmp_path):
    service = _service(tmp_path)
    connection = _connection(service, mode="demo")
    mandate = service.create_mandate(
        connection["id"],
        {
            "mode": "demo", "symbols": ["EURUSD"], "maxOrders": 4,
            "maxMargin": 100, "ttlSeconds": 86400,
            "confirmation": f"MANDATE DEMO {connection['label']}",
        },
    )
    live = _FakeLive({
        "mode": "demo", "connectionId": connection["id"],
        "mandateId": mandate["id"], "symbols": ["EURUSD"],
        "maxOrders": 4, "maxMargin": 100, "botId": None, "pin": "strategy:x:1",
    })

    outcome = renewal.ensure_authority(service, live, "trial_1")
    assert outcome["renewed"] is False
    assert live.trial["mandateId"] == mandate["id"]
    assert len(service.list_mandates(connection["id"])) == 1


def test_an_exhausted_mandate_is_replaced_and_the_trial_moves_with_it(tmp_path):
    service = _service(tmp_path)
    connection = _connection(service, mode="demo")
    old = service.create_mandate(
        connection["id"],
        {
            "mode": "demo", "symbols": ["EURUSD"], "maxOrders": 2,
            "maxMargin": 100, "ttlSeconds": 86400,
            "confirmation": f"MANDATE DEMO {connection['label']}",
        },
    )
    # Spend the only two orders: remainingOrders is now zero.
    service.mandates.consume(old["id"])
    service.mandates.consume(old["id"])

    live = _FakeLive({
        "mode": "demo", "connectionId": connection["id"],
        "mandateId": old["id"], "symbols": ["EURUSD"],
        "maxOrders": 4, "maxMargin": 100, "botId": None, "pin": "strategy:x:1",
    })

    outcome = renewal.ensure_authority(service, live, "trial_1")
    assert outcome["renewed"] is True
    new_id = live.trial["mandateId"]
    assert new_id and new_id != old["id"]

    rows = {row["id"]: row for row in service.list_mandates(connection["id"])}
    assert rows[old["id"]]["revokedAt"] is not None
    assert rows[new_id]["revokedAt"] is None
    assert rows[new_id]["remainingOrders"] == 4
    assert rows[new_id]["mode"] == "demo"

    events = service.connections.list_events(connection["id"])
    assert any(
        event["kind"] == "mandate" and event["outcome"] == "renewed"
        for event in events
    )


def test_a_live_entry_renews_the_arm_before_it_expires(tmp_path):
    service = _service(tmp_path)
    connection = _connection(service, mode="live")
    service.connections.set_arm(connection["id"], until=_iso(600), by="operator")
    mandate = service.create_mandate(
        connection["id"],
        {
            "mode": "live", "symbols": ["EURUSD"], "maxOrders": 4,
            "maxMargin": 100, "ttlSeconds": 86400,
            "confirmation": f"MANDATE LIVE {connection['label']}",
            "workflowId": "strategy:x:1", "workflowVersion": 1,
        },
    )
    live = _FakeLive({
        "mode": "live", "connectionId": connection["id"],
        "mandateId": mandate["id"], "symbols": ["EURUSD"],
        "maxOrders": 4, "maxMargin": 100, "botId": None, "pin": "strategy:x:1",
    })

    outcome = renewal.ensure_authority(service, live, "trial_1")
    assert outcome["arm"]["renewed"] is True
    left = renewal._seconds_left(service.get_connection(connection["id"])["armedUntil"])
    assert left > 7 * 3600
    # A healthy mandate is not replaced just because the arm was extended.
    assert outcome["renewed"] is False
    assert live.trial["mandateId"] == mandate["id"]


def test_a_forced_renewal_of_an_expired_arm_is_a_clear_refusal(tmp_path):
    service = _service(tmp_path)
    connection = _connection(service, mode="live")
    service.connections.set_arm(connection["id"], until=_iso(600), by="operator")
    mandate = service.create_mandate(
        connection["id"],
        {
            "mode": "live", "symbols": ["EURUSD"], "maxOrders": 4,
            "maxMargin": 100, "ttlSeconds": 86400,
            "confirmation": f"MANDATE LIVE {connection['label']}",
            "workflowId": "strategy:x:1", "workflowVersion": 1,
        },
    )
    service.connections.clear_arm(connection["id"])
    live = _FakeLive({
        "mode": "live", "connectionId": connection["id"],
        "mandateId": mandate["id"], "symbols": ["EURUSD"],
        "maxOrders": 4, "maxMargin": 100, "botId": None, "pin": "strategy:x:1",
    })

    with pytest.raises(TradeRefused) as caught:
        renewal.ensure_authority(service, live, "trial_1", force=True)
    assert caught.value.code == "LIVE_ARM_EXPIRED"


def test_a_forced_renewal_replaces_an_otherwise_healthy_mandate(tmp_path):
    service = _service(tmp_path)
    connection = _connection(service, mode="demo")
    old = service.create_mandate(
        connection["id"],
        {
            "mode": "demo", "symbols": ["EURUSD"], "maxOrders": 4,
            "maxMargin": 100, "ttlSeconds": 86400,
            "confirmation": f"MANDATE DEMO {connection['label']}",
        },
    )
    live = _FakeLive({
        "mode": "demo", "connectionId": connection["id"],
        "mandateId": old["id"], "symbols": ["EURUSD"],
        "maxOrders": 4, "maxMargin": 100, "botId": None, "pin": "strategy:x:1",
    })

    outcome = renewal.ensure_authority(service, live, "trial_1", force=True)
    assert outcome["renewed"] is True
    assert live.trial["mandateId"] != old["id"]
