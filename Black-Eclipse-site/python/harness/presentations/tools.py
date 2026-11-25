"""Agent-facing presentation tools.

Four tools on the ONE shared registry:

  * `presentation_create` -- author a deck and export a real `.pptx`
  * `presentation_edit`   -- revision-guarded deck edits
  * `presentation_export` -- render an existing deck to `.pptx`
  * `presentation_outline`-- read a deck's slides (read-only)

A deck is stored as an ordinary Artifact Center artifact; exports are new
artifacts. Nothing here invents data: an unresolved chart is UNAVAILABLE, and an
export reports overflowing slides instead of claiming a clean deck.
"""

from __future__ import annotations

from typing import Any, Optional

from ..tools import ToolContext, ToolDefinition, ToolRegistry
from .import_pptx import deck_from_pptx
from .model import DeckChartSeries, DeckError, DeckSpec, dump_deck, parse_deck
from .ops import DeckOp
from .render import outline as deck_outline
from .render import render_deck
from .service import PresentationService

_LAYOUTS = [
    "title", "section", "agenda", "bullets", "two_column", "statement", "quote",
    "metric_row", "table", "chart", "image_full", "image_right", "closing",
]


def _ok(text: str, data: Optional[dict[str, Any]] = None) -> tuple[str, Any]:
    return text, data or {}


def _err(exc: Exception) -> tuple[str, Any]:
    if isinstance(exc, DeckError):
        return f"{exc.code}: {exc.message}", {"error": exc.code}
    return f"PRESENTATION_ERROR: {exc}", {"error": "PRESENTATION_ERROR"}


def _service(ctx: ToolContext) -> PresentationService:
    artifacts = getattr(ctx, "artifact_service", None)
    if artifacts is None:
        from ..artifacts.bootstrap import get_artifact_service  # noqa: PLC0415

        artifacts = get_artifact_service()
    if artifacts is None:
        raise DeckError("ARTIFACT_UNAVAILABLE", "the artifact service is not available")
    return PresentationService(artifacts)


def _owner(ctx: ToolContext) -> str:
    return str(getattr(ctx, "workspace_id", None) or "local")


def _deck_from_args(args: dict[str, Any]) -> DeckSpec:
    if isinstance(args.get("deck"), dict):
        return parse_deck(args["deck"])
    slides = args.get("slides")
    if not isinstance(slides, list) or not slides:
        raise DeckError("DECK_INVALID", "provide `slides` (at least one) or a full `deck` object")
    payload: dict[str, Any] = {
        "meta": {
            "title": str(args.get("title") or "Untitled deck"),
            "subtitle": args.get("subtitle"),
            "author": args.get("author"),
            "date": args.get("date"),
            "footer": args.get("footer"),
        },
        "size": args.get("size") or "16:9",
        "theme": {"preset": args.get("theme") or "eclipse"},
        "dataSources": args.get("dataSources") or {},
        "slides": slides,
    }
    return parse_deck(payload)


def _rows(series: Any) -> tuple[list[str], list[Optional[float]]]:
    categories: list[str] = []
    values: list[Optional[float]] = []
    for row in series or []:
        if isinstance(row, dict):
            categories.append(str(row.get("date") or row.get("time") or row.get("label") or ""))
            raw = row.get("close") if "close" in row else row.get("value")
        elif isinstance(row, (list, tuple)) and len(row) >= 2:
            categories.append(str(row[0]))
            raw = row[1]
        else:
            continue
        values.append(float(raw) if isinstance(raw, (int, float)) else None)
    return categories, values


async def _prefetch_prices(ctx: ToolContext, deck: DeckSpec) -> dict[str, dict[str, Any]]:
    """Resolve named market sources ONCE per symbol/range, best-effort."""
    bridge = getattr(ctx, "data_bridge", None)
    if bridge is None or not deck.data_sources:
        return {}
    needed: set[str] = set()
    for slide in deck.slides:
        reference = slide.chart.data_source if slide.chart is not None else None
        if reference and reference in deck.data_sources:
            source = deck.data_sources[reference]
            needed.add(f"{source.symbol}|{source.range}|{source.interval}")
    resolved: dict[str, dict[str, Any]] = {}
    for lookup in sorted(needed):
        symbol, range_name, interval = lookup.split("|")
        try:
            result = await bridge.fetch(
                "price_history",
                {"symbol": symbol, "market": "", "range": range_name, "period": interval},
            )
        except Exception:  # noqa: BLE001 - unavailable is a valid outcome
            continue
        if not isinstance(result, dict):
            continue
        categories, values = _rows(result.get("rows") or result.get("series"))
        if not any(value is not None for value in values):
            continue
        resolved[lookup] = {
            "categories": categories,
            "values": values,
            "label": symbol,
            "source": str(result.get("provider") or "market data"),
        }
    return resolved


def _apply_prices(deck: DeckSpec, prices: dict[str, dict[str, Any]]) -> DeckSpec:
    """Store resolved market series in the deck so the artifact is self-contained."""
    for slide in deck.slides:
        chart = slide.chart
        reference = chart.data_source if chart is not None else None
        if chart is None or not reference or reference not in deck.data_sources:
            continue
        source = deck.data_sources[reference]
        hit = prices.get(f"{source.symbol}|{source.range}|{source.interval}")
        if not hit or not hit.get("values"):
            continue
        if not chart.series:
            chart.series = [DeckChartSeries(name=str(hit.get("label") or source.symbol),
                                            data=list(hit["values"]))]
            chart.categories = list(hit.get("categories") or [])
        if not slide.source:
            slide.source = str(hit.get("source") or f"{source.symbol} {source.range}")
    return deck


def _export_note(result: dict[str, Any]) -> str:
    parts = [f"PowerPoint saved at {result.get('path')}"]
    if result.get("overflowSlides"):
        parts.append(
            "overflow on slides " + ", ".join(str(item) for item in result["overflowSlides"])
        )
    return "; ".join(parts)


async def _create(args: dict[str, Any], ctx: ToolContext) -> Any:
    try:
        deck = _deck_from_args(args)
        service = _service(ctx)
        deck = _apply_prices(deck, await _prefetch_prices(ctx, deck))
        snap = service.create(
            deck,
            owner_id=_owner(ctx),
            name=str(args.get("name") or "").strip() or None,
            description=str(args.get("description") or ""),
            conversation_id=getattr(ctx, "conversation_id", None),
            tags=list(args.get("tags") or []),
        )
    except Exception as exc:  # noqa: BLE001 - a builder bug must not crash the run
        return _err(exc)

    data: dict[str, Any] = {"deck": snap.model_dump(by_alias=True), "slides": len(deck.slides)}
    text = (
        f"Created deck {snap.name!r} ({len(deck.slides)} slides) as artifact {snap.artifact_id}."
    )
    reported_id = snap.artifact_id
    if args.get("export", True):
        try:
            exported = service.export(
                snap.artifact_id, owner_id=_owner(ctx), fmt="pptx",
                save_to=args.get("saveTo"), price_data={},
            )
            data.update(
                {
                    "export": exported["artifact"],
                    "path": exported["path"],
                    "warnings": exported["warnings"],
                    "overflowSlides": exported["overflowSlides"],
                }
            )
            text += " " + _export_note(exported)
            reported_id = str(exported["artifact"]["id"])
        except DeckError as exc:
            data["exportError"] = exc.code
            text += f" The deck exists, but the PowerPoint export failed: {exc.code}: {exc.message}"
        except Exception as exc:  # noqa: BLE001
            data["exportError"] = "PRESENTATION_EXPORT_FAILED"
            text += f" The deck exists, but the PowerPoint export failed: {exc}"

    # The same chat hand-over `document_create` uses: the run knows a real file
    # (or at least the deck artifact) exists, with its slide titles.
    from ..documents.authoring import report_authored_document  # noqa: PLC0415

    report_authored_document(
        ctx,
        title=str(deck.meta.title),
        filename=snap.name,
        headings=[str(slide.title or slide.layout.value) for slide in deck.slides],
        artifact_id=reported_id,
        artifact_uri=f"artifact://{reported_id}" if reported_id else "",
    )
    return _ok(text, data)


async def _edit(args: dict[str, Any], ctx: ToolContext) -> Any:
    artifact_id = str(args.get("artifactId") or "").strip()
    if not artifact_id:
        return _err(DeckError("DECK_INVALID", "artifactId is required"))
    try:
        base_version = int(args.get("baseVersion") or 0)
    except (TypeError, ValueError):
        return _err(DeckError("DECK_INVALID", "baseVersion must be an integer"))
    if base_version < 1:
        return _err(DeckError("DECK_INVALID", "baseVersion is required (the version you edited)"))
    raw_ops = args.get("operations")
    if not isinstance(raw_ops, list) or not raw_ops:
        return _err(DeckError("DECK_INVALID", "operations must be a non-empty array"))
    try:
        operations = [DeckOp.model_validate(item) for item in raw_ops]
        service = _service(ctx)
        snap = service.save(
            artifact_id, base_version=base_version, operations=operations,
            owner_id=_owner(ctx), actor="presentation-agent",
            change_note=str(args.get("changeNote") or ""),
        )
        deck = parse_deck(snap.content)
        prices = await _prefetch_prices(ctx, deck)
        model = render_deck(deck, price_resolver=lambda source: prices.get(
            f"{source.get('symbol')}|{source.get('range')}|{source.get('interval')}"
        ))
    except Exception as exc:  # noqa: BLE001
        return _err(exc)
    warnings = model.get("warnings") or []
    text = f"Updated deck {snap.name!r} to version {snap.version} ({len(deck.slides)} slides)."
    if warnings:
        text += f" Warnings: {len(warnings)} (first: {warnings[0]['code']} on {warnings[0]['slideId']})."
    return _ok(text, {
        "deck": snap.model_dump(by_alias=True),
        "warnings": warnings,
        "overflowSlides": model.get("overflowSlides") or [],
    })


async def _export(args: dict[str, Any], ctx: ToolContext) -> Any:
    artifact_id = str(args.get("artifactId") or "").strip()
    if not artifact_id:
        return _err(DeckError("DECK_INVALID", "artifactId is required"))
    try:
        service = _service(ctx)
        deck = service.read_deck(artifact_id, owner_id=_owner(ctx))
        prices = await _prefetch_prices(ctx, deck)
        result = service.export(
            artifact_id, owner_id=_owner(ctx), fmt=args.get("format") or "pptx",
            save_to=args.get("saveTo"), price_data=prices,
            filename=args.get("filename"),
        )
    except Exception as exc:  # noqa: BLE001
        return _err(exc)
    return _ok(
        f"Exported deck as {result['artifact']['name']!r} at {result['path']}. "
        f"Slides: {result['slides']}. "
        + (f"Overflow: {', '.join(result['overflowSlides'])}." if result["overflowSlides"] else "No overflow detected."),
        {
            "artifact": result["artifact"],
            "path": result["path"],
            "slides": result["slides"],
            "warnings": result["warnings"],
            "overflowSlides": result["overflowSlides"],
        },
    )


async def _outline(args: dict[str, Any], ctx: ToolContext) -> Any:
    artifact_id = str(args.get("artifactId") or "").strip()
    if not artifact_id:
        return _err(DeckError("DECK_INVALID", "artifactId is required"))
    try:
        service = _service(ctx)
        snap = service.snapshot(artifact_id, owner_id=_owner(ctx))
        deck = parse_deck(snap.content)
    except Exception as exc:  # noqa: BLE001
        return _err(exc)
    rows = deck_outline(deck)
    lines = [
        f"{index}. [{row['layout']}] {row['title'] or '(untitled)'}"
        for index, row in enumerate(rows, start=1)
    ]
    return _ok(
        f"Deck {snap.name!r}: {len(rows)} slides.\n" + "\n".join(lines),
        {
            "artifact": snap.model_dump(by_alias=True),
            "meta": deck.meta.model_dump(by_alias=True),
            "slides": rows,
            "content": dump_deck(deck),
        },
    )


async def _import(args: dict[str, Any], ctx: ToolContext) -> Any:
    artifact_id = str(args.get("artifactId") or "").strip()
    if not artifact_id:
        return _err(DeckError("DECK_INVALID", "artifactId is required (a .pptx in the Artifact Center)"))
    try:
        service = _service(ctx)
        data = service.read_bytes(artifact_id, owner_id=_owner(ctx))
        deck, warnings = deck_from_pptx(data, title=str(args.get("title") or ""))
        snap = service.create(
            deck, owner_id=_owner(ctx),
            description=f"Imported outline from artifact {artifact_id}",
            conversation_id=getattr(ctx, "conversation_id", None),
            tags=["deck", "imported"],
        )
    except Exception as exc:  # noqa: BLE001
        return _err(exc)
    text = (
        f"Imported {len(deck.slides)} slides from the presentation into deck "
        f"{snap.name!r} (artifact {snap.artifact_id}). This is an editable OUTLINE, not a "
        "lossless copy: design, animations, charts and images are not carried over."
    )
    if warnings:
        text += f" Notes: {'; '.join(warnings[:5])}."
    return _ok(text, {
        "deck": snap.model_dump(by_alias=True),
        "slides": len(deck.slides),
        "warnings": warnings,
    })


def register_presentation_tools(registry: ToolRegistry) -> list[str]:
    from .service import PPTX_MIME  # noqa: F401, PLC0415 - keeps the MIME near the tool

    names: list[str] = []

    if registry.get("presentation_create") is None:
        registry.register(ToolDefinition(
            name="presentation_create",
            description=(
                "Create a real presentation (deck) and export a .pptx. The deck is stored as an "
                "editable, versioned artifact; the PowerPoint file is a derivative saved to the "
                "Artifact Center and Documents/Eclipse (or `saveTo`), and the result names its "
                "ABSOLUTE path. Provide a `title` and `slides` (or a full `deck`). Each slide has "
                "a `layout` (title/section/agenda/bullets/two_column/statement/quote/metric_row/"
                "table/chart/image_full/image_right/closing), a `title`, and the fields that "
                "layout uses: bullets, left/right {heading,bullets}, metrics [{label,value,delta,"
                "note,source}], table {headers,rows,caption}, chart {kind,categories,series,"
                "dataSource,valueFormat}, image {artifactId,alt,fit}, notes, source. Use "
                "`dataSources` for named market series: {\"aapl\": {\"kind\":\"price\",\"symbol\":"
                "\"AAPL\",\"range\":\"1y\",\"interval\":\"1d\"}} then chart.dataSource=\"aapl\". "
                "NEVER invent a number: a chart with no resolvable data is exported as an explicit "
                "UNAVAILABLE slide. Text that does not fit is reported in overflowSlides, never "
                "silently clipped."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "subtitle": {"type": "string"},
                    "author": {"type": "string"},
                    "date": {"type": "string"},
                    "footer": {"type": "string"},
                    "name": {"type": "string", "description": "Artifact name; defaults to the title."},
                    "description": {"type": "string"},
                    "tags": {"type": "array", "items": {"type": "string"}},
                    "size": {"type": "string", "enum": ["16:9", "4:3"]},
                    "theme": {"type": "string", "enum": ["eclipse", "paper", "midnight", "slate"]},
                    "saveTo": {
                        "type": "string",
                        "description": "Where the readable .pptx copy goes: 'documents' (default), 'downloads', 'desktop', or an absolute folder path.",
                    },
                    "export": {"type": "boolean", "description": "Export a .pptx immediately (default true)."},
                    "dataSources": {"type": "object", "description": "Named market series, keyed by chart.dataSource."},
                    "deck": {"type": "object", "description": "A full DeckSpec; wins over title/slides."},
                    "slides": {
                        "type": "array",
                        "description": "Slides in order.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "id": {"type": "string"},
                                "layout": {"type": "string", "enum": _LAYOUTS},
                                "title": {"type": "string"},
                                "subtitle": {"type": "string"},
                                "eyebrow": {"type": "string"},
                                "bullets": {"type": "array", "items": {"type": "string"}},
                                "body": {"type": "string"},
                                "statement": {"type": "string"},
                                "attribution": {"type": "string"},
                                "left": {"type": "object"},
                                "right": {"type": "object"},
                                "metrics": {"type": "array", "items": {"type": "object"}},
                                "table": {"type": "object"},
                                "chart": {"type": "object"},
                                "image": {"type": "object"},
                                "notes": {"type": "string"},
                                "source": {"type": "string"},
                            },
                            "required": ["layout"],
                        },
                    },
                },
                "required": [],
            },
            handler=_create,
            permission="read",
            risk="low",
            timeout_ms=90_000,
        ))
        names.append("presentation_create")

    if registry.get("presentation_edit") is None:
        registry.register(ToolDefinition(
            name="presentation_edit",
            description=(
                "Edit a deck artifact with typed operations under a revision guard. Supply "
                "artifactId, baseVersion (the version you read) and operations. Ops: add_slide "
                "{at,slide}, delete_slide {slideId}, move_slide {slideId,to}, replace_slide "
                "{slideId,slide}, set_meta {field,value}, set_theme {preset,accent}, set_notes "
                "{slideId,text}, set_title {slideId,text}, set_bullets {slideId,bullets}, "
                "set_body {slideId,text}, set_source {slideId,text}, set_chart {slideId,chart}, "
                "set_table {slideId,table}, set_metrics {slideId,metrics}. A stale baseVersion is "
                "refused (409) rather than overwriting someone else's edit."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "artifactId": {"type": "string"},
                    "baseVersion": {"type": "integer"},
                    "changeNote": {"type": "string"},
                    "operations": {
                        "type": "array",
                        "items": {"type": "object", "description": "A DeckOp object (see description)."},
                    },
                },
                "required": ["artifactId", "baseVersion", "operations"],
            },
            handler=_edit,
            permission="read",
            risk="low",
            timeout_ms=60_000,
        ))
        names.append("presentation_edit")

    if registry.get("presentation_export") is None:
        registry.register(ToolDefinition(
            name="presentation_export",
            description=(
                "Export an existing deck artifact to a real PowerPoint file (format 'pptx'), a PDF "
                "('pdf'), or a ZIP of per-slide PNG images ('png'). The file is registered in the "
                "Artifact Center and written to Documents/Eclipse (or `saveTo`); the result names its "
                "ABSOLUTE path. The source deck is never modified. A missing writer library is "
                "reported honestly, never replaced by a fake file."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "artifactId": {"type": "string"},
                    "format": {"type": "string", "enum": ["pptx", "pdf", "png"]},
                    "saveTo": {"type": "string"},
                    "filename": {"type": "string"},
                },
                "required": ["artifactId"],
            },
            handler=_export,
            permission="read",
            risk="low",
            timeout_ms=90_000,
        ))
        names.append("presentation_export")

    if registry.get("presentation_outline") is None:
        registry.register(ToolDefinition(
            name="presentation_outline",
            description=(
                "Read a deck artifact's slides (id, layout, title, speaker notes present) and its "
                "canonical JSON. Read-only; use it before editing so operations address real slide ids."
            ),
            input_schema={
                "type": "object",
                "properties": {"artifactId": {"type": "string"}},
                "required": ["artifactId"],
            },
            handler=_outline,
            permission="read",
            risk="low",
            timeout_ms=20_000,
        ))
        names.append("presentation_outline")

    if registry.get("presentation_import") is None:
        registry.register(ToolDefinition(
            name="presentation_import",
            description=(
                "Import an existing PowerPoint file already in the Artifact Center as an editable "
                "deck OUTLINE. Supply its `artifactId` (and an optional `title`). Titles, text lines "
                "(as bullets), table rows (as text) and speaker notes are kept; design, masters, "
                "animations, charts and images are NOT carried over, and the result says so in its "
                "warnings. The new deck is an ordinary editable/presentable artifact."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "artifactId": {"type": "string"},
                    "title": {"type": "string"},
                },
                "required": ["artifactId"],
            },
            handler=_import,
            permission="read",
            risk="low",
            timeout_ms=60_000,
        ))
        names.append("presentation_import")

    return names


__all__ = ["register_presentation_tools"]
