"""Automation `messaging` delivery goes through the real channel gateway."""

from __future__ import annotations

import asyncio

from harness.automation.delivery import deliver_automation
from harness.automation.model import AutomationDefinition


class _Store:
    def __init__(self) -> None:
        self.rows: list[dict] = []

    def record_delivery(self, execution_id, **kwargs):
        self.rows.append({"executionId": execution_id, **kwargs})


class _Bus:
    def __init__(self) -> None:
        self.events: list[dict] = []

    def publish(self, event):
        self.events.append(event)


class _Channels:
    def __init__(self, ok: bool = True) -> None:
        self.calls: list[tuple[str, str, str]] = []
        self.ok = ok

    async def send_outbound(self, kind, external_chat, text):
        self.calls.append((kind, external_chat, text))
        return {"ok": self.ok, **({} if self.ok else {"reason": "transport down"})}


def _definition(destination: str) -> AutomationDefinition:
    return AutomationDefinition(
        id="auto_delivery",
        name="Morning brief",
        state="active",
        trigger={"type": "manual"},
        action={"type": "run_agent", "objective": "brief me"},
        delivery={"targets": [
            {"adapter": "messaging", "destination": destination},
        ]},
        created_at="2026-11-26T00:00:00Z",
        updated_at="2026-11-26T00:00:00Z",
    )


def test_messaging_destination_reaches_the_channel_gateway():
    store, bus, channels = _Store(), _Bus(), _Channels()
    outcomes = asyncio.run(deliver_automation(
        _definition("telegram:12345"),
        execution_id="exec_1", run_id="run_1", state="completed",
        output="Nothing material changed.\n\nSpreads were quiet.",
        store=store, bus=bus, channel_service=channels,
    ))
    assert outcomes[0]["ok"] is True
    assert channels.calls and channels.calls[0][0] == "telegram"
    assert channels.calls[0][1] == "12345"
    assert "Finished as asked." in channels.calls[0][2]
    assert "Spreads were quiet." in channels.calls[0][2]
    assert bus.events == []  # the dead template event is no longer the path
    assert store.rows[0]["ok"] is True


def test_channel_failure_is_reported_not_raised():
    store, bus, channels = _Store(), _Bus(), _Channels(ok=False)
    outcomes = asyncio.run(deliver_automation(
        _definition("slack:C123"),
        execution_id="exec_2", run_id=None, state="failed",
        output=None, store=store, bus=bus, channel_service=channels,
    ))
    assert outcomes[0]["ok"] is False
    assert outcomes[0]["reasonCode"] == "DELIVERY_FAILED"
    assert outcomes[0]["reason"] == "transport down"


def test_malformed_destination_falls_back_to_the_bus_contract():
    store, bus, channels = _Store(), _Bus(), _Channels()
    outcomes = asyncio.run(deliver_automation(
        _definition("no-colon-here"),
        execution_id="exec_3", run_id="run_3", state="completed",
        output="ok", store=store, bus=bus, channel_service=channels,
    ))
    assert outcomes[0]["ok"] is True
    assert channels.calls == []
    assert bus.events and bus.events[0]["type"] == "automation.message"
