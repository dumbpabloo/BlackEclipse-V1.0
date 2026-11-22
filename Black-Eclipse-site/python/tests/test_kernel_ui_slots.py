"""Kernel UI slots: registration, rendering, exact unload and API."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from harness.kernel.services.runtime import kernel_runtime
from harness.kernel.services.ui import UISlotService, slot_for_ui_kind
from harness.routers.kernel_ui import router
from harness.tools import ToolRegistry


@pytest.fixture()
def runtime():
    kernel_runtime.reset(enabled=True, registry=ToolRegistry())
    yield kernel_runtime
    kernel_runtime.reset(enabled=False)


def test_slot_register_render_unregister(runtime):
    service = UISlotService()
    undo = service.register(
        "chat.sidebar", "demo",
        lambda: {"type": "markdown", "text": "hello"},
        title="Demo", plugin_id="p",
    )
    assert service.entries("chat.sidebar")[0]["title"] == "Demo"
    assert service.render("chat.sidebar", "demo") == {"type": "markdown", "text": "hello"}
    undo()
    assert service.entries("chat.sidebar") == []
    with pytest.raises(KeyError):
        service.render("chat.sidebar", "demo")


def test_ui_kind_maps_to_a_slot():
    assert slot_for_ui_kind("panel") == "plugins.panel"
    assert slot_for_ui_kind("sidebar") == "chat.sidebar"
    assert slot_for_ui_kind("settings") == "settings.section"


def test_kernel_runtime_registers_a_slot(runtime):
    undo = runtime.register_slot(
        "chat.toolbar", "clock", lambda: {"type": "status", "text": "ok"},
        title="Clock", plugin_id="demo",
    )
    assert runtime.ui_slots.render("chat.toolbar", "clock")["text"] == "ok"
    undo()
    assert runtime.ui_slots.entries("chat.toolbar") == []


def test_slot_api_lists_and_renders(runtime):
    runtime.register_slot("chat.sidebar", "demo", lambda: {"type": "markdown", "text": "x"}, title="Demo")
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as client:
        listed = client.get("/kernel/ui/slots")
        assert listed.status_code == 200
        assert listed.json()["enabled"] is True
        assert any(row["id"] == "demo" for row in listed.json()["entries"])
        rendered = client.get("/kernel/ui/slots/chat.sidebar/demo")
        assert rendered.status_code == 200
        assert rendered.json()["text"] == "x"
        missing = client.get("/kernel/ui/slots/chat.sidebar/nope")
        assert missing.status_code == 404
