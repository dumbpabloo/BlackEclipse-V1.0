"""PDF reports and the `eclipse_report` tool — real bytes, honest failures."""

from __future__ import annotations

import asyncio

import pytest

from harness.artifacts import ArtifactService, ArtifactStore, LocalArtifactStorage
from harness.documents.reporting import build_pdf, build_report
from harness.documents.tools import register_document_authoring_tools
from harness.persistence import AgentMemoryStore
from harness.tools import ToolContext, ToolRegistry

BLOCKS = [
    {"type": "heading", "text": "Ulcer Index", "level": 1},
    {"type": "paragraph", "text": "Downside risk computed from the recorded price series."},
    {"type": "bullets", "items": ["Prices: 100, 90, 80", "Ulcer Index: 12.909944"]},
    {"type": "table", "headers": ["Metric", "Value"], "rows": [["Ulcer Index", 12.909944], ["Points", 3]]},
]


@pytest.fixture()
def ctx(tmp_path, monkeypatch):
    monkeypatch.setenv("ECLIPSE_OUTPUT_DIR", str(tmp_path / "exports"))
    store = AgentMemoryStore(str(tmp_path / "agent-memory.db"))
    registry = ToolRegistry()
    register_document_authoring_tools(registry)
    service = ArtifactService(ArtifactStore(store), LocalArtifactStorage(tmp_path / "files"))
    context = ToolContext(
        run_id="run_report", log=lambda *a, **k: None, memory_store=store,
        artifact_service=service, agent_id="eclipse-agent",
    )
    yield registry, context, service
    store.close()


def _run(registry, context, name, args):
    tool = registry.get(name)
    assert tool is not None
    return asyncio.run(tool.handler(args, context))


def test_pdf_builder_produces_real_pdf_bytes():
    data = build_pdf(BLOCKS, title="Report", subtitle="Computed by Eclipse")
    assert data.startswith(b"%PDF-")
    assert len(data) > 500


def test_build_report_dispatches_pdf_and_docx():
    assert build_report("pdf", title="R", blocks=BLOCKS).startswith(b"%PDF-")
    assert build_report("docx", title="R", blocks=BLOCKS).startswith(b"PK")


@pytest.mark.parametrize("block", [
    {"type": "table", "rows": [["A", "B"], [1, 2]]},
    {"type": "table", "headers": ["A", "B"], "rows": [{"A": 1, "B": 2}]},
    {"type": "table", "headers": ["A", "B"], "rows": [[1]]},
    {"type": "table", "headers": ["A", "B"], "rows": []},
])
def test_pdf_table_accepts_model_authored_shapes(block):
    data = build_pdf([block], title="Table shapes")
    assert data.startswith(b"%PDF-")


def test_eclipse_report_registers_a_pdf_artifact(ctx):
    registry, context, _service = ctx
    text, data = _run(registry, context, "eclipse_report", {
        "title": "Ulcer Index Report",
        "format": "pdf",
        "blocks": BLOCKS,
        "tags": ["analysis"],
    })
    assert "Created PDF report" in text
    artifact = data["artifact"]
    assert artifact["name"] == "Ulcer-Index-Report.pdf"
    assert artifact["mimeType"] == "application/pdf"
    assert artifact["category"] == "document"
    assert artifact["sizeBytes"] > 500
    assert data["bytes"] > 500


def test_eclipse_report_can_write_docx_and_xlsx(ctx):
    registry, context, _service = ctx
    _text, docx = _run(registry, context, "eclipse_report", {
        "title": "Word report", "format": "docx",
        "blocks": [{"type": "paragraph", "text": "Body"}],
    })
    assert docx["artifact"]["name"].endswith(".docx")
    _text, xlsx = _run(registry, context, "eclipse_report", {
        "title": "Data report", "format": "xlsx",
        "sheets": [{"name": "Results", "rows": [["Metric", "Value"], ["Ulcer", 12.9]]}],
    })
    assert xlsx["artifact"]["name"].endswith(".xlsx")


def test_eclipse_report_refuses_empty_or_unsupported_content(ctx):
    registry, context, _service = ctx
    empty = _run(registry, context, "eclipse_report", {"title": "Empty", "format": "pdf"})
    assert empty[1]["error"] == "REPORT_EMPTY"
    unsupported = _run(registry, context, "eclipse_report", {
        "title": "Bad", "format": "exe", "blocks": BLOCKS,
    })
    assert unsupported[1]["error"] == "DOCUMENT_FORMAT_UNSUPPORTED"
