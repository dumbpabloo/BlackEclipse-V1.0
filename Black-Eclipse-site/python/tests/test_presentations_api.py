"""Presentation API — theme tokens, deck export, honest refusals."""

from __future__ import annotations

import json
import os
import tempfile

os.environ.setdefault(
    "AGENT_MEMORY_DB_PATH",
    os.path.join(tempfile.mkdtemp(prefix="be-deck-api-"), "agent-memory.db"),
)
os.environ.setdefault(
    "HARNESS_ARTIFACT_ROOT",
    os.path.join(tempfile.mkdtemp(prefix="be-deck-files-"), "artifacts"),
)
os.environ.setdefault("ECLIPSE_OUTPUT_DIR", tempfile.mkdtemp(prefix="be-deck-exports-"))

from fastapi.testclient import TestClient  # noqa: E402

from harness.main import app  # noqa: E402

client = TestClient(app)

DECK = {
    "schemaVersion": 1,
    "meta": {"title": "API Deck"},
    "slides": [
        {"layout": "title", "title": "API Deck"},
        {"layout": "bullets", "title": "Points", "bullets": ["One", "Two"]},
    ],
}


def _create_deck() -> str:
    response = client.post("/canvas/artifacts", json={
        "name": "API Deck",
        "kind": "presentation",
        "content": json.dumps(DECK),
    })
    assert response.status_code == 201, response.text
    snap = response.json()
    assert snap["kind"] == "presentation"
    assert snap["name"].endswith(".deck.json")
    return snap["artifactId"]


def test_themes_endpoint_returns_the_presets():
    response = client.get("/presentations/themes")
    assert response.status_code == 200, response.text
    names = [theme["preset"] for theme in response.json()["themes"]]
    assert names == ["eclipse", "paper", "midnight", "slate"]
    eclipse = response.json()["themes"][0]
    assert eclipse["background"] == "#0B0F1A"
    assert eclipse["type"]["title"] == 34


def test_export_endpoint_produces_a_pptx_artifact():
    artifact_id = _create_deck()
    response = client.post(f"/presentations/{artifact_id}/export", json={"format": "pptx"})
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["slides"] == 2
    assert data["path"].endswith(".pptx")
    assert data["artifact"]["mimeType"].endswith("presentationml.presentation")
    assert data["overflowSlides"] == []

    download = client.get(f"/artifacts/{data['artifact']['id']}/download")
    assert download.status_code == 200
    assert download.content[:2] == b"PK"  # a real OOXML ZIP


def test_canvas_save_accepts_deck_ops_by_kind():
    artifact_id = _create_deck()
    saved = client.put(f"/canvas/artifacts/{artifact_id}", json={
        "baseVersion": 1,
        "operations": [{"op": "set_title", "slideId": "s1", "text": "Via API"}],
    })
    assert saved.status_code == 200, saved.text
    assert saved.json()["version"] == 2
    assert "Via API" in saved.json()["content"]

    stale = client.put(f"/canvas/artifacts/{artifact_id}", json={
        "baseVersion": 1,
        "operations": [{"op": "set_title", "slideId": "s1", "text": "Nope"}],
    })
    assert stale.status_code == 409

    bad = client.put(f"/canvas/artifacts/{artifact_id}", json={
        "baseVersion": 2,
        "operations": [{"op": "append", "content": "text op on a deck"}],
    })
    assert bad.status_code == 400


def test_export_endpoint_supports_pdf_and_png():
    artifact_id = _create_deck()
    for fmt, mime, suffix, magic in (
        ("pdf", "application/pdf", ".pdf", b"%PDF"),
        ("png", "application/zip", ".zip", b"PK"),
    ):
        response = client.post(f"/presentations/{artifact_id}/export", json={"format": fmt})
        assert response.status_code == 200, response.text
        data = response.json()
        assert data["artifact"]["mimeType"] == mime
        assert data["path"].endswith(suffix)
        download = client.get(f"/artifacts/{data['artifact']['id']}/download")
        assert download.status_code == 200
        assert download.content.startswith(magic)


def test_export_refuses_a_non_deck_artifact():
    created = client.post("/canvas/artifacts", json={
        "name": "Note", "kind": "document", "content": "# Note",
    })
    artifact_id = created.json()["artifactId"]
    response = client.post(f"/presentations/{artifact_id}/export", json={"format": "pptx"})
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "DECK_INVALID"


def test_export_missing_artifact_is_404():
    response = client.post("/presentations/does-not-exist/export", json={"format": "pptx"})
    assert response.status_code == 404
