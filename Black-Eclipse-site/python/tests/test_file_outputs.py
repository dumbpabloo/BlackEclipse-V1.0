"""The user-facing output folder and `file_locate` — real paths, honest misses."""

from __future__ import annotations

import asyncio
import os
import time

import pytest

from harness.documents import outputs
from harness.documents import locate_tools
from harness.documents.locate_tools import register_file_locate_tool
from harness.tools import ToolContext, ToolRegistry


@pytest.fixture()
def export_dir(tmp_path, monkeypatch):
    folder = tmp_path / "Documents" / "Eclipse"
    monkeypatch.setenv(outputs.OUTPUT_DIR_ENV, str(folder))
    return folder


def test_publish_writes_a_real_file_and_returns_an_absolute_path(export_dir):
    path = outputs.publish(b"%PDF-1.4 hello", "Q3 review.pdf")
    assert os.path.isabs(str(path))
    assert path.parent == export_dir.resolve()
    assert path.read_bytes() == b"%PDF-1.4 hello"


def test_publish_never_collides_or_escapes_the_folder(export_dir):
    first = outputs.publish(b"one", "report.pdf")
    second = outputs.publish(b"two", "report.pdf")
    assert first != second
    assert second.name == "report (2).pdf"

    escaped = outputs.publish(b"x", "..\\..\\evil.pdf")
    assert escaped.parent == export_dir.resolve()
    assert ".." not in escaped.name


def test_locate_finds_the_newest_match_by_extension(export_dir, tmp_path, monkeypatch):
    old = outputs.publish(b"old", "alpha.pdf")
    time.sleep(0.01)
    new = outputs.publish(b"new", "beta.pdf")
    outputs.publish(b"sheet", "gamma.xlsx")

    monkeypatch.setattr(outputs, "search_roots", lambda: [export_dir])
    result = outputs.locate("", extensions=["pdf"], limit=10)
    assert [item["path"] for item in result["files"]] == [str(new), str(old)]


def test_locate_is_a_bounded_miss_not_a_guess(export_dir, monkeypatch):
    monkeypatch.setattr(outputs, "search_roots", lambda: [export_dir])
    result = outputs.locate("nothing-like-this", limit=5)
    assert result["files"] == []
    assert result["roots"] == [str(export_dir)]


def test_file_locate_tool_answers_with_absolute_paths(export_dir, monkeypatch):
    target = outputs.publish(b"%PDF-1.4", "Ulcer-Index-Report.pdf")
    monkeypatch.setattr(locate_tools, "search_roots", lambda: [export_dir])
    registry = ToolRegistry()
    assert register_file_locate_tool(registry) == ["file_locate"]
    tool = registry.get("file_locate")
    ctx = ToolContext(run_id="r", log=lambda *a, **k: None)
    text, data = asyncio.run(tool.handler({"query": "ulcer"}, ctx))
    assert str(target) in text
    assert data["count"] == 1
    assert data["files"][0]["path"] == str(target)


def test_document_result_names_the_absolute_path(tmp_path, monkeypatch):
    from harness.artifacts import ArtifactService, ArtifactStore, LocalArtifactStorage
    from harness.documents.tools import register_document_authoring_tools
    from harness.persistence import AgentMemoryStore

    monkeypatch.setenv(outputs.OUTPUT_DIR_ENV, str(tmp_path / "exports"))
    store = AgentMemoryStore(str(tmp_path / "agent-memory.db"))
    registry = ToolRegistry()
    register_document_authoring_tools(registry)
    service = ArtifactService(ArtifactStore(store), LocalArtifactStorage(tmp_path / "files"))
    ctx = ToolContext(
        run_id="r", log=lambda *a, **k: None, memory_store=store,
        artifact_service=service, agent_id="eclipse-agent",
    )
    tool = registry.get("eclipse_report")
    chosen = tmp_path / "Desktop"
    text, data = asyncio.run(tool.handler({
        "title": "Ulcer Index",
        "format": "pdf",
        "blocks": [{"type": "paragraph", "text": "Body"}],
        "saveTo": str(chosen),
    }, ctx))
    assert data["path"] and os.path.isabs(data["path"])
    assert data["path"] in text
    assert os.path.isfile(data["path"])
    assert os.path.dirname(data["path"]) == str(chosen.resolve())
    store.close()
