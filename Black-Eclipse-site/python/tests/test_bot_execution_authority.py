"""P5: the leash on a bot's unattended execution.

`l0` proposes, `l1` fills demo, `l2` fills live — and only with a mandate pinned
to the bot AND the objective version the operator approved. Every refusal here is
a named code, because "why did my bot not trade?" must have an answer.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from harness.automation.model import ExecutionAuthority
from harness.automation.tools import _create, _update, set_automation_service
from harness.trade.authority import (
    check_live_order,
    check_mandate_creation,
    risk_amount,
)
from harness.trade.model import TradeRefused


class _Automation:
    def __init__(self, definition: object | None) -> None:
        self._definition = definition
        self.updated: dict | None = None

    def get(self, bot_id: str):
        return self._definition

    def update(self, bot_id: str, patch: dict, *, updated_by: str | None = None):
        self.updated = patch
        return SimpleNamespace(public=lambda: {"id": bot_id})


def _bot(tier: str = "l2", version: int = 3, **authority) -> SimpleNamespace:
    return SimpleNamespace(
        id="auto_1",
        version=version,
        execution_authority=ExecutionAuthority(tier=tier, **authority),
    )


class _Brokers:
    def __init__(
        self, *, positions: list[dict] | None = None, last_order: str | None = None,
        positions_ok: bool = True,
    ) -> None:
        self._positions = positions if positions is not None else []
        self._last_order = last_order
        self._positions_ok = positions_ok

    def _connection(self, connection_id: str):
        return SimpleNamespace(id=connection_id, config={})

    def _magic(self, connection) -> int:
        return 7

    def positions(self, connection_id: str):
        if not self._positions_ok:
            return {"available": False, "data": None}
        return {"available": True, "data": {"positions": self._positions}}

    def ledger_orders(self, connection_id=None, limit=20, automation_id=None):
        if self._last_order:
            return [{"createdAt": self._last_order, "automationId": automation_id}]
        return []


def _iso(seconds_ago: int) -> str:
    moment = datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)
    return moment.isoformat().replace("+00:00", "Z")


def _install(definition) -> None:
    set_automation_service(_Automation(definition))


def _check(brokers, *, tier="l2", mode="live", intent=None, bot="auto_1", **authority):
    _install(_bot(tier=tier, **authority))
    return check_live_order(
        brokers,
        automation_id=bot,
        connection_id="brk_1",
        mode=mode,
        intent=intent if intent is not None else {"symbol": "EURUSD", "stopLoss": 1.08},
    )


def test_the_default_authority_is_propose_only():
    authority = ExecutionAuthority()
    assert authority.tier == "l0"
    assert authority.appetite == "normal"
    assert authority.sizing_mode == "risk_based"
    assert authority.require_stop is True


def test_demo_orders_are_not_this_gates_business():
    assert _check(_Brokers(), mode="demo") is None


def test_an_unknown_mode_is_refused():
    refusal = _check(_Brokers(), mode="")
    assert refusal is not None and refusal[1]["reason"] == "EXECUTION_DISABLED"


def test_live_from_a_non_bot_run_is_refused():
    refusal = _check(_Brokers(), bot="")
    assert refusal is not None and refusal[1]["reason"] == "LIVE_REQUIRES_BOT"


def test_live_without_a_readable_definition_is_refused():
    set_automation_service(_Automation(None))
    refusal = check_live_order(
        _Brokers(), automation_id="auto_1", connection_id="brk_1",
        mode="live", intent={"stopLoss": 1.0},
    )
    assert refusal is not None and refusal[1]["reason"] == "BOT_UNAVAILABLE"


def test_l0_and_l1_cannot_place_live_orders():
    for tier in ("l0", "l1"):
        refusal = _check(_Brokers(), tier=tier)
        assert refusal is not None, tier
        assert refusal[1]["reason"] == "BOT_TIER_INSUFFICIENT"
        assert tier.upper() in refusal[0]


def test_l2_requires_a_stop_when_configured():
    refusal = _check(_Brokers(), intent={"symbol": "EURUSD"})
    assert refusal is not None and refusal[1]["reason"] == "STOP_REQUIRED"


def test_an_unreadable_position_book_fails_closed():
    refusal = _check(_Brokers(positions_ok=False))
    assert refusal is not None and refusal[1]["reason"] == "POSITIONS_UNAVAILABLE"


def test_the_position_cap_counts_only_eclipse_positions():
    positions = [
        {"magic": 7}, {"magic": 7}, {"magic": 0},  # the last is a manual trade
    ]
    refusal = _check(_Brokers(positions=positions), max_open_positions=2)
    assert refusal is not None and refusal[1]["reason"] == "POSITION_CAP_REACHED"
    # A manual position does not consume the bot's cap.
    assert _check(_Brokers(positions=[{"magic": 7}, {"magic": 0}]), max_open_positions=2) is None


def test_a_recent_order_trips_the_cooldown():
    refusal = _check(_Brokers(last_order=_iso(30)), cooldown_s=900)
    assert refusal is not None and refusal[1]["reason"] == "COOLDOWN_ACTIVE"
    assert _check(_Brokers(last_order=_iso(2000)), cooldown_s=900) is None


def test_an_authorized_l2_bot_passes_the_gate():
    assert _check(_Brokers(), tier="l2") is None


def test_mandate_creation_requires_l2_and_the_current_version():
    _install(_bot(tier="l0", version=3))
    with pytest.raises(TradeRefused) as exc:
        check_mandate_creation(_Brokers(), bot_id="auto_1", bot_version=3, max_margin=100)
    assert exc.value.code == "BOT_TIER_INSUFFICIENT"

    _install(_bot(tier="l2", version=4))
    with pytest.raises(TradeRefused) as exc:
        check_mandate_creation(_Brokers(), bot_id="auto_1", bot_version=3, max_margin=100)
    assert exc.value.code == "BOT_VERSION_STALE"


def test_a_mandate_may_not_exceed_the_bots_margin_ceiling(monkeypatch):
    _install(_bot(tier="l2", version=3, max_margin_per_order=50))
    with pytest.raises(TradeRefused) as exc:
        check_mandate_creation(_Brokers(), bot_id="auto_1", bot_version=3, max_margin=100)
    assert exc.value.code == "MANDATE_ABOVE_BOT_CAP"
    # This test is about the MARGIN ceiling; the proof gate and the live ramp are
    # covered in test_trade_proof.py / test_trade_ramp.py. Stub both so the
    # success path is reachable.
    monkeypatch.setattr("harness.trade.proof.live_proof_gap", lambda *_a, **_k: None)
    monkeypatch.setattr("harness.trade.ramp.ramp_gap", lambda *_a, **_k: None)
    check_mandate_creation(_Brokers(), bot_id="auto_1", bot_version=3, max_margin=50)


def test_risk_amount_follows_the_band_and_never_goes_negative():
    assert risk_amount(10_000, "conservative") == pytest.approx(25.0)
    assert risk_amount(10_000, "normal") == pytest.approx(50.0)
    assert risk_amount(10_000, "aggressive") == pytest.approx(100.0)
    assert risk_amount(10_000, "nonsense") == pytest.approx(50.0)
    assert risk_amount(-5, "normal") == 0.0
    assert risk_amount("not-a-number", "normal") == 0.0


def _ctx(approvals=None, advisory=None) -> SimpleNamespace:
    return SimpleNamespace(
        run_id="run_1", approvals=approvals, advisory_service=advisory,
        conversation_id="bot-auto_1",
    )


def test_a_new_bot_cannot_be_created_above_l0():
    service = _Automation(None)
    service.created = None

    def create(payload, *, created_by=None):
        service.created = payload
        return SimpleNamespace(public=lambda: {"id": "auto_1"})

    service.create = create
    set_automation_service(service)
    try:
        text, payload = asyncio.run(_create(
            {"objective": "trade", "executionAuthority": {"tier": "l2"}}, _ctx()
        ))
        assert payload["created"] is False
        assert payload["reason"] == "tier_requires_existing_bot"
        assert service.created is None
    finally:
        set_automation_service(None)


def test_raising_the_tier_needs_an_operator_approval():
    # No approval store: raising authority is refused, never assumed.
    service = _Automation(_bot(tier="l0"))
    set_automation_service(service)
    try:
        text, payload = asyncio.run(_update(
            {"botId": "auto_1", "executionAuthority": {"tier": "l1"}}, _ctx()
        ))
        assert payload["updated"] is False
        assert payload["approved"] is False
        assert service.updated is None
        assert "approval" in text
    finally:
        set_automation_service(None)


def test_l2_is_refused_without_a_scored_record_even_with_an_approval_store():
    class _Approvals:
        def consume_kind(self, **kwargs):
            return {"id": "appr_1"}  # would approve, if it were ever asked

    advisory = SimpleNamespace(
        list_decisions=lambda **kwargs: [],
        store=SimpleNamespace(outcomes_for=lambda ids: []),
    )
    service = _Automation(_bot(tier="l0"))
    set_automation_service(service)
    try:
        text, payload = asyncio.run(_update(
            {"botId": "auto_1", "executionAuthority": {"tier": "l2"}},
            _ctx(approvals=_Approvals(), advisory=advisory),
        ))
        assert payload["updated"] is False
        assert payload["reason"] == "track_record_insufficient"
        assert service.updated is None
    finally:
        set_automation_service(None)


def test_lowering_the_tier_needs_no_approval():
    class _Service(_Automation):
        pass

    service = _Service(_bot(tier="l2"))
    set_automation_service(service)
    try:
        asyncio.run(_update(
            {"botId": "auto_1", "executionAuthority": {"tier": "l0"}}, _ctx()
        ))
        assert service.updated is not None
        assert service.updated["executionAuthority"]["tier"] == "l0"
    finally:
        set_automation_service(None)
