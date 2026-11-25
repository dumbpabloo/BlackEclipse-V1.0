"""Presentations — contract, layout, ops, export, service and tools.

The deck is JSON; the pptx is a derivative. These tests pin both: a deck that
validates, a layout that reports overflow instead of clipping, ops that respect
ids and limits, and an export that reopens as a real presentation.
"""

from __future__ import annotations

import asyncio
import io
import json
import zipfile

import pytest
from pptx import Presentation

from harness.artifacts import ArtifactService, ArtifactStore, LocalArtifactStorage
from harness.canvas.model import CanvasConflict, CanvasKind
from harness.persistence import AgentMemoryStore
from harness.presentations.export_pptx import export_pptx
from harness.presentations.export_pdf import export_pdf, export_png_zip
from harness.presentations.import_pptx import deck_from_pptx
from harness.presentations.model import (
    MAX_BULLET_CHARS,
    DeckError,
    SlideLayout,
    dump_deck,
    parse_deck,
)
from harness.presentations.ops import DeckOp, apply_deck_ops
from harness.presentations.render import outline, render_deck
from harness.presentations.service import PresentationService
from harness.presentations.tools import register_presentation_tools
from harness.tools import ToolContext, ToolRegistry


def _deck(**overrides):
    payload = {
        "meta": {"title": "Q3 Update", "footer": "Confidential"},
        "theme": {"preset": "eclipse"},
        "slides": [
            {"layout": "title", "title": "Q3 Update", "subtitle": "BlackEclipse Capital"},
            {"layout": "agenda", "title": "Agenda", "bullets": ["Results", "Outlook"]},
            {"layout": "bullets", "title": "Highlights", "bullets": ["Revenue up", "Costs flat"]},
            {
                "layout": "metric_row",
                "title": "By the numbers",
                "metrics": [{"label": "Revenue", "value": "$12.4M", "delta": "+18% YoY",
                             "source": "10-Q 2026-09-30"}],
            },
            {
                "layout": "chart",
                "title": "Revenue trend",
                "chart": {
                    "kind": "line",
                    "categories": ["Jan", "Feb", "Mar"],
                    "series": [{"name": "Revenue", "data": [10, 11, 12]}],
                },
                "source": "JDdS daily close",
            },
            {
                "layout": "table",
                "title": "Comps",
                "table": {"headers": ["Ticker", "P/E"], "rows": [["AAPL", "31.2"]]},
            },
            {"layout": "closing", "title": "Thank you", "subtitle": "Questions?"},
        ],
    }
    payload.update(overrides)
    return payload


@pytest.fixture()
def service(tmp_path, monkeypatch):
    monkeypatch.setenv("ECLIPSE_OUTPUT_DIR", str(tmp_path / "exports"))
    store = AgentMemoryStore(str(tmp_path / "agent-memory.db"))
    artifacts = ArtifactService(ArtifactStore(store), LocalArtifactStorage(tmp_path / "files"))
    yield PresentationService(artifacts), artifacts
    store.close()


@pytest.fixture()
def ctx(tmp_path, monkeypatch):
    monkeypatch.setenv("ECLIPSE_OUTPUT_DIR", str(tmp_path / "exports"))
    store = AgentMemoryStore(str(tmp_path / "agent-memory.db"))
    registry = ToolRegistry()
    register_presentation_tools(registry)
    artifacts = ArtifactService(ArtifactStore(store), LocalArtifactStorage(tmp_path / "files"))
    context = ToolContext(
        run_id="run_deck", log=lambda *a, **k: None, memory_store=store,
        artifact_service=artifacts, agent_id="eclipse-agent",
    )
    yield registry, context, artifacts
    store.close()


def _run(registry, context, name, args):
    tool = registry.get(name)
    assert tool is not None
    return asyncio.run(tool.handler(args, context))


# ── contract ──────────────────────────────────────────────────────────────────


def test_contract_accepts_a_full_deck_and_assigns_ids():
    deck = parse_deck(_deck())
    assert len(deck.slides) == 7
    assert [slide.id for slide in deck.slides] == [f"s{i}" for i in range(1, 8)]
    assert deck.slides[3].layout is SlideLayout.METRIC_ROW
    assert "schemaVersion" in dump_deck(deck)


def test_contract_refuses_empty_duplicate_and_unknown():
    with pytest.raises(DeckError) as exc:
        parse_deck({"slides": []})
    assert exc.value.code == "DECK_INVALID"

    with pytest.raises(DeckError):
        parse_deck({"slides": [{"id": "x", "layout": "bullets", "bullets": ["a"]},
                               {"id": "x", "layout": "bullets", "bullets": ["b"]}]})

    with pytest.raises(DeckError):
        parse_deck({"slides": [{"layout": "bullets", "titel": "typo"}]})

    with pytest.raises(DeckError):
        parse_deck({"slides": [{"layout": "nope"}]})


def test_contract_refuses_layout_specific_missing_content():
    with pytest.raises(DeckError):
        parse_deck({"slides": [{"layout": "chart", "title": "x"}]})
    with pytest.raises(DeckError):
        parse_deck({"slides": [{"layout": "table", "title": "x"}]})
    with pytest.raises(DeckError):
        parse_deck({"slides": [{"layout": "metric_row", "title": "x"}]})


def test_contract_bounds_bullets():
    with pytest.raises(DeckError):
        parse_deck({"slides": [{"layout": "bullets", "bullets": ["x"] * 8}]})
    with pytest.raises(DeckError):
        parse_deck({"slides": [{"layout": "bullets", "bullets": ["x" * (MAX_BULLET_CHARS + 1)]}]})


# ── layout / render ───────────────────────────────────────────────────────────


def test_render_model_has_regions_sizes_and_pages():
    model = render_deck(parse_deck(_deck()))
    assert model["slideCount"] == 7
    first = model["slides"][0]
    assert first["layout"] == "title"
    assert set(first["regions"]) >= {"title", "subtitle", "footer"}
    assert first["page"] == 1
    assert model["slides"][3]["fonts"]["metric_value"] >= 18
    assert model["size"]["widthIn"] == pytest.approx(13.333)
    assert model["theme"]["preset"] == "eclipse"


def test_overflow_is_reported_by_the_fit_engine_but_legal_decks_fit():
    # The contract's own limits (7 bullets x 180 chars) are chosen so a legal
    # deck still fits at the smallest readable size; the overflow path is the
    # engine's honest fallback, exercised directly here.
    from harness.presentations.fit import fit_text

    pt, fits = fit_text("word " * 400, 4.0, 0.4, 30)
    assert fits is False
    assert pt >= 10

    deck = parse_deck({"slides": [{
        "layout": "bullets",
        "title": "Heavy but legal",
        "bullets": [("word " * 34).strip() for _ in range(7)],
    }]})
    model = render_deck(deck)
    assert model["slides"][0]["warnings"] == []
    assert model["overflowSlides"] == []


def test_unresolved_chart_is_unavailable():
    deck = parse_deck({
        "dataSources": {"aapl": {"kind": "price", "symbol": "AAPL"}},
        "slides": [{
            "layout": "chart",
            "title": "Trend",
            "chart": {"kind": "line", "dataSource": "missing"},
        }],
    })
    model = render_deck(deck)
    chart = model["slides"][0]["chart"]
    assert chart["available"] is False
    assert "chart_unavailable" in model["slides"][0]["warnings"]


def test_outline_lists_slides():
    rows = outline(parse_deck(_deck()))
    assert rows[0]["title"] == "Q3 Update"
    assert rows[4]["layout"] == "chart"


# ── ops ───────────────────────────────────────────────────────────────────────


def test_ops_add_move_edit_and_delete_slides():
    content = dump_deck(parse_deck(_deck()))
    content = apply_deck_ops(content, [DeckOp.model_validate({"op": "add_slide", "at": 1, "slide": {
        "layout": "section", "title": "New section"}})])
    deck = parse_deck(content)
    assert deck.slides[1].title == "New section"
    new_id = deck.slides[1].id

    content = apply_deck_ops(content, [DeckOp.model_validate({"op": "move_slide", "slideId": new_id, "to": 0})])
    assert parse_deck(content).slides[0].id == new_id

    content = apply_deck_ops(content, [DeckOp.model_validate(
        {"op": "set_title", "slideId": new_id, "text": "Renamed"}
    )])
    assert parse_deck(content).slides[0].title == "Renamed"

    content = apply_deck_ops(content, [DeckOp.model_validate({"op": "delete_slide", "slideId": new_id})])
    assert new_id not in [slide.id for slide in parse_deck(content).slides]


def test_ops_refuse_unknown_slide_and_last_delete():
    content = dump_deck(parse_deck(_deck()))
    with pytest.raises(DeckError) as exc:
        apply_deck_ops(content, [DeckOp.model_validate(
            {"op": "set_title", "slideId": "nope", "text": "x"}
        )])
    assert exc.value.code == "DECK_SLIDE_NOT_FOUND"

    one = dump_deck(parse_deck({"slides": [{"layout": "bullets", "bullets": ["a"]}]}))
    with pytest.raises(DeckError) as exc:
        apply_deck_ops(one, [DeckOp.model_validate({"op": "delete_slide", "slideId": "s1"})])
    assert exc.value.code == "DECK_LIMIT"


def test_ops_set_name_and_notes():
    content = dump_deck(parse_deck(_deck()))
    content = apply_deck_ops(content, [
        DeckOp.model_validate({"op": "set_meta", "field": "title", "value": "Board deck"}),
        DeckOp.model_validate({"op": "set_notes", "slideId": "s3", "text": "Say this slowly."}),
    ])
    deck = parse_deck(content)
    assert deck.meta.title == "Board deck"
    assert deck.slides[2].notes == "Say this slowly."


# ── export ────────────────────────────────────────────────────────────────────


def test_export_produces_a_real_presentation_with_notes_and_shapes():
    deck = parse_deck(_deck())
    deck.slides[2].notes = "Speaker note for highlights."
    model = render_deck(deck)
    data, summary = export_pptx(deck, model)
    presentation = Presentation(io.BytesIO(data))
    assert len(presentation.slides) == 7
    assert summary["slides"] == 7
    assert presentation.slides[2].notes_slide.notes_text_frame.text == "Speaker note for highlights."
    # The table slide has a graphic frame with a table.
    shapes = list(presentation.slides[5].shapes)
    assert any(getattr(shape, "has_table", False) for shape in shapes)
    # The chart slide has a chart.
    assert any(getattr(shape, "has_chart", False) for shape in presentation.slides[4].shapes)
    assert summary["overflowSlides"] == []


def test_export_marks_an_unavailable_chart_without_faking_a_series():
    deck = parse_deck({"slides": [{
        "layout": "chart", "title": "Trend",
        "chart": {"kind": "line", "dataSource": "missing"},
    }]})
    model = render_deck(deck)
    data, summary = export_pptx(deck, model)
    presentation = Presentation(io.BytesIO(data))
    assert len(presentation.slides) == 1
    assert "chart_unavailable" in [w["code"] for w in summary["warnings"]]
    assert not any(getattr(shape, "has_chart", False) for shape in presentation.slides[0].shapes)


def test_import_pptx_builds_an_editable_outline():
    from pptx import Presentation as Pptx

    source = Pptx()
    first = source.slides.add_slide(source.slide_layouts[1])
    first.shapes.title.text = "Strategy"
    first.placeholders[1].text = "Grow revenue\nControl costs"
    first.notes_slide.notes_text_frame.text = "Say this slowly."
    second = source.slides.add_slide(source.slide_layouts[5])  # title only
    second.shapes.title.text = "Thank you"
    buffer = io.BytesIO()
    source.save(buffer)

    deck, warnings = deck_from_pptx(buffer.getvalue(), title="Imported")
    assert deck.meta.title == "Imported"
    assert len(deck.slides) == 2
    assert deck.slides[0].title == "Strategy"
    assert deck.slides[0].bullets == ["Grow revenue", "Control costs"]
    assert deck.slides[0].notes == "Say this slowly."
    assert deck.slides[1].layout.value == "section"
    assert warnings == []


def test_tool_import_reads_a_pptx_artifact(ctx):
    from pptx import Presentation as Pptx

    registry, context, artifacts = ctx
    source = Pptx()
    slide = source.slides.add_slide(source.slide_layouts[1])
    slide.shapes.title.text = "Agenda"
    slide.placeholders[1].text = "One\nTwo"
    buffer = io.BytesIO()
    source.save(buffer)
    artifact = artifacts.register_bytes(
        buffer.getvalue(), name="source.pptx", owner_id="local",
        mime_type="application/vnd.openxmlformats-officedocument.presentationml.presentation",
    )

    text, data = _run(registry, context, "presentation_import", {"artifactId": artifact.id})
    assert "Imported 1 slides" in text
    assert "OUTLINE" in text
    assert data["deck"]["kind"] == "presentation"
    assert data["slides"] == 1


# ── exports beyond pptx ──────────────────────────────────────────────────────


def test_export_pdf_has_one_page_per_slide():
    import pypdfium2 as pdfium

    deck = parse_deck(_deck())
    model = render_deck(deck)
    data, summary = export_pdf(deck, model)
    assert data.startswith(b"%PDF")
    assert len(data) > 5_000
    assert summary["slides"] == 7
    pdf = pdfium.PdfDocument(data)
    assert len(pdf) == 7


def test_export_png_zip_has_one_png_per_slide():
    deck = parse_deck(_deck())
    model = render_deck(deck)
    data, summary = export_png_zip(deck, model)
    assert summary["slides"] == 7
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        names = archive.namelist()
        assert len(names) == 7
        assert names[0] == "slide-01.png"
        assert archive.read(names[0])[:8] == b"\x89PNG\r\n\x1a\n"


def test_export_pdf_survives_an_unavailable_chart_and_image():
    deck = parse_deck({"slides": [
        {"layout": "chart", "title": "Trend", "chart": {"kind": "line", "dataSource": "missing"}},
        {"layout": "image_full", "title": "Photo", "image": {"alt": "x", "fit": "cover"}},
    ]})
    model = render_deck(deck)
    data, _summary = export_pdf(deck, model)
    assert data.startswith(b"%PDF")


# ── service ───────────────────────────────────────────────────────────────────


def test_service_creates_a_canvas_presentation_and_exports(service):
    presentations, artifacts = service
    snap = presentations.create(parse_deck(_deck()), owner_id="local")
    assert snap.kind is CanvasKind.PRESENTATION
    assert snap.name.endswith(".deck.json")
    assert snap.content.count('"layout"') == 7

    result = presentations.export(snap.artifact_id, owner_id="local")
    assert result["slides"] == 7
    assert result["path"].endswith(".pptx")
    exported = artifacts.get(result["artifact"]["id"], owner_id="local")
    assert exported.mime_type.endswith("presentationml.presentation")
    assert exported.metadata["derivedFrom"] == {"artifactId": snap.artifact_id, "version": 1}

    reopened = Presentation(io.BytesIO(artifacts.open_content(result["artifact"]["id"], owner_id="local")[0].read()))
    assert len(reopened.slides) == 7


def test_service_export_formats(service):
    presentations, artifacts = service
    snap = presentations.create(parse_deck(_deck()), owner_id="local")
    pdf = presentations.export(snap.artifact_id, owner_id="local", fmt="pdf")
    assert pdf["path"].endswith(".pdf")
    assert pdf["artifact"]["mimeType"] == "application/pdf"
    assert pdf["slides"] == 7

    png = presentations.export(snap.artifact_id, owner_id="local", fmt="png")
    assert png["path"].endswith(".zip")
    assert png["artifact"]["mimeType"] == "application/zip"
    stored = artifacts.open_content(png["artifact"]["id"], owner_id="local")[0].read()
    with zipfile.ZipFile(io.BytesIO(stored)) as archive:
        assert len(archive.namelist()) == 7

    with pytest.raises(DeckError) as exc:
        presentations.export(snap.artifact_id, owner_id="local", fmt="docx")
    assert exc.value.code == "PRESENTATION_FORMAT_UNSUPPORTED"


def test_service_edit_is_revision_guarded(service):
    presentations, _artifacts = service
    snap = presentations.create(parse_deck(_deck()), owner_id="local")
    op = DeckOp.model_validate({"op": "set_title", "slideId": "s3", "text": "Edited"})
    updated = presentations.save(snap.artifact_id, base_version=1, operations=[op], owner_id="local")
    assert updated.version == 2
    assert "Edited" in updated.content
    with pytest.raises(CanvasConflict) as exc:
        presentations.save(snap.artifact_id, base_version=1, operations=[op], owner_id="local")
    assert exc.value.current_version == 2


def test_service_outline_reports_real_slide_ids(service):
    presentations, _artifacts = service
    snap = presentations.create(parse_deck(_deck()), owner_id="local")
    data = presentations.outline(snap.artifact_id, owner_id="local")
    assert [row["id"] for row in data["slides"]] == [f"s{i}" for i in range(1, 8)]


def test_presentation_name_keeps_the_deck_extension_with_a_dotted_title(service):
    presentations, _artifacts = service
    snap = presentations.create(
        parse_deck({"meta": {"title": "Q3.2026 Update"},
                    "slides": [{"layout": "title", "title": "Q3.2026 Update"}]}),
        owner_id="local",
    )
    assert snap.name.endswith(".deck.json")


def test_deck_artifact_has_a_rendered_thumbnail(service):
    presentations, artifacts = service
    snap = presentations.create(parse_deck(_deck()), owner_id="local")
    preview = artifacts.preview(snap.artifact_id, owner_id="local")
    assert preview["thumbnailKey"]
    found = artifacts.open_thumbnail(snap.artifact_id, owner_id="local")
    assert found is not None
    handle, _artifact = found
    try:
        assert handle.read(8) == b"\x89PNG\r\n\x1a\n"
    finally:
        handle.close()


def test_deck_artifact_preview_is_a_readable_outline(service):
    presentations, artifacts = service
    snap = presentations.create(parse_deck(_deck()), owner_id="local")
    preview = artifacts.preview(snap.artifact_id, owner_id="local")
    assert preview["kind"] == "json"
    assert "Q3 Update" in preview["text"]
    assert "5. [chart] Revenue trend" in preview["text"]


def test_canvas_text_ops_refuse_decks():
    from harness.canvas.ops import OpError, apply_operations

    with pytest.raises(OpError) as exc:
        apply_operations("{}", [], kind="presentation")
    assert "do not apply to decks" in exc.value.message


# ── AI deck proposals ────────────────────────────────────────────────────────

SETTINGS = {"provider": "openai", "model": "gpt-4o", "apiKey": "test-key"}


def _fake_deck_model(monkeypatch, payload: str):
    from harness.canvas.edits import CanvasEditOrchestrator

    async def fake(_settings, _prompt):
        return payload

    monkeypatch.setattr(CanvasEditOrchestrator, "_call", staticmethod(fake))


def test_deck_ai_proposal_returns_typed_ops_and_applies_them(service, monkeypatch):
    from harness.canvas.edits import CanvasEditOrchestrator
    from harness.canvas.model import CanvasEditRequest, CanvasMode, SaveCanvasInput
    from harness.canvas.service import CanvasService

    presentations, artifacts = service
    snap = presentations.create(parse_deck(_deck()), owner_id="local")
    _fake_deck_model(monkeypatch, json.dumps({
        "answer": "",
        "summary": "Rename the title slide",
        "operations": [{"op": "set_title", "slideId": "s1", "text": "AI title"}],
        "notes": [],
    }))

    proposal = asyncio.run(CanvasEditOrchestrator(CanvasService(artifacts)).propose(
        snap.artifact_id,
        CanvasEditRequest(base_version=1, instruction="rename the title", mode=CanvasMode.EDIT,
                          model_settings=SETTINGS),
        owner_id="local",
    ))
    assert proposal.operations == []
    assert proposal.deck_operations == [{"op": "set_title", "slideId": "s1", "text": "AI title"}]
    assert "AI title" in (proposal.result_content or "")

    saved = CanvasService(artifacts).save(
        snap.artifact_id,
        SaveCanvasInput(base_version=1, operations=proposal.deck_operations),
        owner_id="local",
    )
    assert saved.version == 2
    assert "AI title" in saved.content


def test_deck_ai_proposal_ask_mode_discards_ops(service, monkeypatch):
    from harness.canvas.edits import CanvasEditOrchestrator
    from harness.canvas.model import CanvasEditRequest, CanvasMode
    from harness.canvas.service import CanvasService

    presentations, artifacts = service
    snap = presentations.create(parse_deck(_deck()), owner_id="local")
    _fake_deck_model(monkeypatch, json.dumps({
        "answer": "It has seven slides.",
        "operations": [{"op": "set_title", "slideId": "s1", "text": "X"}],
        "notes": [],
    }))
    proposal = asyncio.run(CanvasEditOrchestrator(CanvasService(artifacts)).propose(
        snap.artifact_id,
        CanvasEditRequest(base_version=1, instruction="how many slides?", mode=CanvasMode.ASK,
                          model_settings=SETTINGS),
        owner_id="local",
    ))
    assert proposal.deck_operations == []
    assert proposal.result_content is None
    assert any("Ask mode" in note for note in proposal.notes)


def test_deck_ai_proposal_rejects_an_unknown_slide(service, monkeypatch):
    from harness.canvas.edits import CanvasEditOrchestrator
    from harness.canvas.model import CanvasEditRequest, CanvasError, CanvasMode
    from harness.canvas.service import CanvasService

    presentations, artifacts = service
    snap = presentations.create(parse_deck(_deck()), owner_id="local")
    _fake_deck_model(monkeypatch, json.dumps({
        "operations": [{"op": "set_title", "slideId": "nope", "text": "X"}],
    }))
    with pytest.raises(CanvasError) as exc:
        asyncio.run(CanvasEditOrchestrator(CanvasService(artifacts)).propose(
            snap.artifact_id,
            CanvasEditRequest(base_version=1, instruction="edit", mode=CanvasMode.EDIT,
                              model_settings=SETTINGS),
            owner_id="local",
        ))
    assert exc.value.code == "CANVAS_INVALID_PROPOSAL"


def test_save_with_deck_ops_rejects_a_text_op(service):
    from harness.canvas.model import CanvasError, SaveCanvasInput
    from harness.canvas.service import CanvasService

    presentations, artifacts = service
    snap = presentations.create(parse_deck(_deck()), owner_id="local")
    with pytest.raises(CanvasError):
        CanvasService(artifacts).save(
            snap.artifact_id,
            SaveCanvasInput(base_version=1, operations=[{"op": "append", "content": "x"}]),
            owner_id="local",
        )


# ── tools ────────────────────────────────────────────────────────────────────


def test_tool_create_exports_and_writes_a_real_file(ctx):
    registry, context, artifacts = ctx
    text, data = _run(registry, context, "presentation_create", {
        "title": "Tool deck",
        "slides": [
            {"layout": "title", "title": "Tool deck"},
            {"layout": "bullets", "title": "Points", "bullets": ["One", "Two"]},
        ],
    })
    assert "Created deck" in text
    assert "PowerPoint saved at" in text
    assert data["path"].endswith(".pptx")
    deck_artifact = data["deck"]
    assert deck_artifact["kind"] == "presentation"
    assert data["export"]["name"].endswith(".pptx")


def test_tool_outline_and_edit(ctx):
    registry, context, _artifacts = ctx
    _text, data = _run(registry, context, "presentation_create", {
        "title": "Editable", "export": False,
        "slides": [{"layout": "bullets", "title": "A", "bullets": ["x"]}],
    })
    artifact_id = data["deck"]["artifactId"]

    text, outline_data = _run(registry, context, "presentation_outline", {"artifactId": artifact_id})
    assert "1 slides" in text
    assert outline_data["slides"][0]["id"] == "s1"

    text, edited = _run(registry, context, "presentation_edit", {
        "artifactId": artifact_id, "baseVersion": 1,
        "operations": [{"op": "set_title", "slideId": "s1", "text": "B"}],
    })
    assert "version 2" in text
    assert edited["deck"]["version"] == 2


def test_tool_create_reports_export_failure_without_losing_the_deck(ctx, monkeypatch):
    registry, context, _artifacts = ctx
    import harness.presentations.service as service_module

    def boom(*args, **kwargs):
        raise DeckError("PRESENTATION_LIBRARY_MISSING", "python-pptx is not installed")

    monkeypatch.setattr(service_module.PresentationService, "export", boom)
    text, data = _run(registry, context, "presentation_create", {
        "title": "No export", "slides": [{"layout": "title", "title": "No export"}],
    })
    assert "Created deck" in text
    assert "export failed" in text
    assert data["exportError"] == "PRESENTATION_LIBRARY_MISSING"
    assert data["deck"]["artifactId"]
