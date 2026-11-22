"""Gap #7b: a code task accepts bounded reference images.

The chat path already carried `RunImage`; code tasks dropped them. This asserts
the HTTP route builds the run with the images intact (the runtime then attaches
them to the model turn through the existing `RunInput.images` path).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import harness.routers.code as code_router
from harness.code.tasks import TaskStore
from harness.persistence import AgentMemoryStore


class _FakeService:
    def __init__(self, store: AgentMemoryStore) -> None:
        self.store = store
        self.captured = None
        self._tasks = TaskStore(store)

    def code_tasks(self) -> TaskStore:
        return self._tasks

    async def run(self, request):
        self.captured = request
        return SimpleNamespace(model_dump=lambda: {"status": "completed"})


@pytest.fixture()
def client(tmp_path, monkeypatch):
    store = AgentMemoryStore(str(tmp_path / "agent-memory.db"))
    fake = _FakeService(store)
    monkeypatch.setattr(code_router, "service", fake)
    app = FastAPI()
    app.include_router(code_router.router)
    app.dependency_overrides[code_router.require_operator] = lambda: True
    try:
        yield TestClient(app), fake
    finally:
        store.close()


def test_a_code_task_passes_attached_images_into_the_run(client):
    http, fake = client
    image = {
        "dataUrl": "data:image/png;base64," + "A" * 48,
        "caption": "the broken layout",
        "mime": "image/png",
    }
    response = http.post("/code/tasks", json={
        "query": "fix the layout in this screenshot",
        "callbackBaseUrl": "http://localhost:3000",
        "modelSettings": {"provider": "openai", "model": "gpt-4o"},
        "autonomy": "edit_verify",
        "images": [image],
    })
    assert response.status_code == 200, response.text
    assert fake.captured is not None
    images = fake.captured.input.images
    assert images is not None and len(images) == 1
    assert images[0].caption == "the broken layout"
    assert images[0].mime == "image/png"
    assert images[0].data_url.startswith("data:image/png;base64,")


def test_a_code_task_without_images_is_unchanged(client):
    http, fake = client
    response = http.post("/code/tasks", json={
        "query": "rename the helper",
        "callbackBaseUrl": "http://localhost:3000",
        "modelSettings": {"provider": "openai", "model": "gpt-4o"},
        "autonomy": "edit_verify",
    })
    assert response.status_code == 200, response.text
    assert fake.captured.input.images is None


def test_too_many_images_is_rejected(client):
    http, _fake = client
    image = {"dataUrl": "data:image/png;base64," + "A" * 48, "mime": "image/png"}
    response = http.post("/code/tasks", json={
        "query": "fix it",
        "callbackBaseUrl": "http://localhost:3000",
        "modelSettings": {"provider": "openai", "model": "gpt-4o"},
        "autonomy": "edit_verify",
        "images": [image] * 5,
    })
    assert response.status_code == 422
