"""Document Authoring — builders + the `document_create` agent tool."""

from __future__ import annotations

import asyncio
import io
import zipfile

import pytest

from harness.artifacts import ArtifactService, ArtifactStore, LocalArtifactStorage
from harness.documents import (
    DocumentError,
    build,
    build_csv,
    build_docx,
    build_json,
    build_markdown,
    build_pptx,
    build_xlsx,
    canonical_format,
    slug_filename,
)
from harness.documents.tools import register_document_authoring_tools
from harness.persistence import AgentMemoryStore
from harness.tools import ToolContext, ToolRegistry


@pytest.fixture()
def ctx(tmp_path, monkeypatch):
    monkeypatch.setenv("ECLIPSE_OUTPUT_DIR", str(tmp_path / "exports"))
    store = AgentMemoryStore(str(tmp_path / "agent-memory.db"))
    registry = ToolRegistry()
    register_document_authoring_tools(registry)
    service = ArtifactService(ArtifactStore(store), LocalArtifactStorage(tmp_path / "files"))
    context = ToolContext(
        run_id="run_test", log=lambda *a, **k: None, memory_store=store,
        artifact_service=service, agent_id="eclipse-agent",
    )
    yield registry, context, service
    store.close()


def _run(registry, context, name, args):
    tool = registry.get(name)
    assert tool is not None
    return asyncio.run(tool.handler(args, context))


def _parts(data: bytes) -> set[str]:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        return set(archive.namelist())


def test_canonical_format_and_slug():
    assert canonical_format("md") == "markdown"
    assert canonical_format("Text") == "txt"
    assert canonical_format("DOCX") == "docx"
    with pytest.raises(DocumentError) as exc:
        canonical_format("exe")
    assert exc.value.code == "DOCUMENT_FORMAT_UNSUPPORTED"
    assert slug_filename("Q3 Earnings!", "docx") == "Q3-Earnings.docx"
    assert slug_filename("", "csv") == "document.csv"


def test_build_docx_is_valid_ooxml():
    data = build_docx(
        [
            {"type": "heading", "text": "Summary", "level": 2},
            {"type": "paragraph", "text": "Revenue grew."},
            {"type": "bullets", "items": ["one", "two"]},
            {"type": "table", "headers": ["A", "B"], "rows": [[1, 2], [3, 4]]},
        ],
        title="Report",
    )
    parts = _parts(data)
    assert "word/document.xml" in parts
    assert "word/styles.xml" in parts


def test_build_xlsx_is_valid_ooxml():
    data = build_xlsx([{"name": "PnL", "rows": [["Symbol", "P&L"], ["AAPL", 12.5]]}], title="Book")
    parts = _parts(data)
    assert any(p.startswith("xl/worksheets/sheet") for p in parts)
    assert "xl/workbook.xml" in parts


def test_build_pptx_is_valid_ooxml():
    data = build_pptx(
        [{"title": "Intro", "bullets": ["a", "b"], "notes": "speaker"}], title="Deck"
    )
    parts = _parts(data)
    assert any(p.endswith(".xml") for p in parts)
    assert any(p.startswith("ppt/slides/slide") for p in parts)


def test_build_text_formats():
    markdown = build_markdown(
        [{"type": "heading", "text": "Hi", "level": 1}, {"type": "bullets", "items": ["x"]}],
        title="Doc",
    ).decode()
    assert markdown.startswith("# Doc")
    assert "- x" in markdown

    csv_bytes = build_csv([["a", "b"], [1, 2]]).decode()
    assert csv_bytes.strip() == "a,b\r\n1,2"

    json_bytes = build_json([{"a": 1}, {"a": 2}]).decode()
    assert '"a": 1' in json_bytes

    assert build("txt", title="T", blocks=[{"type": "paragraph", "text": "hello"}]).decode().strip()


def test_document_create_registers_artifact(ctx):
    registry, context, _service = ctx
    text, data = _run(registry, context, "document_create", {
        "format": "docx",
        "title": "Q3 Report",
        "blocks": [{"type": "paragraph", "text": "Body text 12345"}],
        "tags": ["report"],
    })
    assert "Created docx document" in text
    artifact = data["artifact"]
    assert artifact["name"] == "Q3-Report.docx"
    assert artifact["category"] == "document"
    assert artifact["mimeType"].endswith("wordprocessingml.document")
    assert artifact["sizeBytes"] > 0
    assert artifact["storageKey"]


def test_document_create_derives_format_from_filename(ctx):
    registry, context, _service = ctx
    text, data = _run(registry, context, "document_create", {
        "filename": "book.xlsx",
        "sheets": [{"name": "S", "rows": [["h"], [1]]}],
    })
    assert "Created xlsx document" in text
    assert data["artifact"]["category"] == "spreadsheet"


def test_document_create_unknown_format_reports_error(ctx):
    registry, context, _service = ctx
    text, data = _run(registry, context, "document_create", {"format": "exe", "title": "x"})
    assert "DOCUMENT_FORMAT_UNSUPPORTED" in text
    assert data["error"] == "DOCUMENT_FORMAT_UNSUPPORTED"


def test_document_create_without_service_reports_unavailable(tmp_path, monkeypatch):
    from harness.artifacts import bootstrap

    monkeypatch.setattr(bootstrap, "get_artifact_service", lambda: None)
    store = AgentMemoryStore(str(tmp_path / "m.db"))
    registry = ToolRegistry()
    register_document_authoring_tools(registry)
    context = ToolContext(run_id="r", log=lambda *a, **k: None, memory_store=store)
    text, data = _run(registry, context, "document_create", {
        "format": "markdown", "title": "T", "blocks": [{"type": "paragraph", "text": "x"}],
    })
    assert "ARTIFACT_UNAVAILABLE" in text
    assert data["error"] == "ARTIFACT_UNAVAILABLE"
    store.close()
