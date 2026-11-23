"""`document_create` — the agent-facing document AUTHORING tool.

Registers on the SAME shared `ToolRegistry` as every other toolset, so validation,
timeout, event stream and permissions apply unchanged. It builds a real file
(Word .docx / Excel .xlsx / PowerPoint .pptx, or Markdown / CSV / JSON / .txt) from
a neutral content vocabulary and records it in the Artifact Center, where the user
can preview and download it.

It writes NO arbitrary filesystem path and executes NO office macro: the bytes go
straight into the confined artifact store through the ONE `ArtifactService`. When
that service (or a required writer library) is absent it reports an honest
`ARTIFACT_UNAVAILABLE` / `DOCUMENT_LIBRARY_MISSING` rather than a fake file.
"""

from __future__ import annotations

from typing import Any, Optional

from ..tools import ToolContext, ToolDefinition, ToolRegistry
from .authoring import (
    FORMAT_EXTENSION,
    FORMAT_MIME,
    DocumentError,
    block_headings,
    build,
    canonical_format,
    report_authored_document,
    slug_filename,
)
from .outputs import publish_copy

_SUPPORTED_FORMATS = ["docx", "xlsx", "pptx", "markdown", "md", "csv", "json", "txt", "text"]


def _ok(text: str, data: Optional[dict[str, Any]] = None) -> tuple[str, Any]:
    return text, data or {}


def _err(exc: Exception) -> tuple[str, Any]:
    if isinstance(exc, DocumentError):
        return f"{exc.code}: {exc.message}", {"error": exc.code}
    return f"DOCUMENT_ERROR: {exc}", {"error": "DOCUMENT_ERROR"}


def _service(ctx: ToolContext) -> Any:
    service = getattr(ctx, "artifact_service", None)
    if service is None:
        from ..artifacts.bootstrap import get_artifact_service  # noqa: PLC0415

        service = get_artifact_service()
    if service is None:
        raise DocumentError("ARTIFACT_UNAVAILABLE", "the artifact service is not available")
    return service


def _owner(ctx: ToolContext) -> str:
    return str(getattr(ctx, "workspace_id", None) or "local")


def _with_extension(name: str, extension: str) -> str:
    lowered = name.casefold()
    return name if lowered.endswith(f".{extension}") else f"{name}.{extension}"


async def _create(args: dict[str, Any], ctx: ToolContext) -> Any:
    raw_format = str(args.get("format") or "").strip()
    if not raw_format:
        filename = str(args.get("filename") or "")
        raw_format = filename.rsplit(".", 1)[-1] if "." in filename else ""
    title = str(args.get("title") or "").strip()
    try:
        canonical = canonical_format(raw_format)
        data = build(
            canonical,
            title=title,
            blocks=args.get("blocks"),
            sheets=args.get("sheets"),
            slides=args.get("slides"),
            rows=args.get("rows"),
        )
    except DocumentError as exc:
        return _err(exc)
    except Exception as exc:  # noqa: BLE001 - a builder bug must not crash the run
        return _err(exc)

    extension = FORMAT_EXTENSION[canonical]
    filename = str(args.get("filename") or "").strip() or slug_filename(title, extension)
    filename = _with_extension(filename, extension)
    try:
        service = _service(ctx)
        artifact = service.register_bytes(
            data,
            name=filename,
            owner_id=_owner(ctx),
            description=str(args.get("description") or f"Generated {canonical} document"),
            mime_type=FORMAT_MIME[canonical],
            source_tool="document_create",
            agent_id=str(getattr(ctx, "agent_id", None) or "eclipse-agent"),
            conversation_id=getattr(ctx, "conversation_id", None),
            run_id=getattr(ctx, "run_id", None),
            tags=list(args.get("tags") or []),
        )
    except Exception as exc:  # noqa: BLE001
        return _err(exc)

    # The writer knows the headings it wrote: record them so a contract's
    # required sections can be checked against the document (W1.3), with the
    # artifact id so the chat can offer the file.
    report_authored_document(
        ctx, title=title, filename=filename, headings=block_headings(args.get("blocks")),
        artifact_id=str(getattr(artifact, "id", "") or ""),
        artifact_uri=f"artifact://{getattr(artifact, 'id', '')}" if getattr(artifact, "id", "") else "",
    )

    path, note = publish_copy(data, filename, args.get("saveTo"))
    return _ok(
        f"Created {canonical} document {artifact.name!r} "
        f"({artifact.size_bytes or len(data)} bytes) as artifact {artifact.id}.{note}",
        {"artifact": artifact.model_dump(by_alias=True), "bytes": len(data), "path": path},
    )


def register_document_authoring_tools(registry: ToolRegistry) -> list[str]:
    """Register the authoring tool on the shared registry. Returns the names."""
    from .locate_tools import register_file_locate_tool  # noqa: PLC0415

    if registry.get("document_create") is not None:
        from .report_tools import register_report_tools  # noqa: PLC0415
        from .edit.tools import register_document_edit_tools  # noqa: PLC0415

        return [
            *register_report_tools(registry),
            *register_document_edit_tools(registry),
            *register_file_locate_tool(registry),
        ]
    registry.register(ToolDefinition(
        name="document_create",
        description=(
            "Create a downloadable document the user asked for, save it to the "
            "Eclipse Artifact Center AND write a readable copy to Documents/Eclipse "
            "(or `saveTo`); the result names its ABSOLUTE path. Builds REAL files: "
            "Word (.docx), Excel (.xlsx), PowerPoint (.pptx), and also Markdown, CSV, "
            "JSON or plain text. Provide a `format` and a `title`, plus the content "
            "for that format: `blocks` (heading/paragraph/bullets/numbers/table/"
            "page_break) for docx/markdown/txt; `sheets` [{name, rows}] for xlsx; "
            "`slides` [{title, bullets, notes}] for pptx; `rows` for csv/json. Never "
            "claim a document was created unless this tool returns success — a "
            "missing writer library or artifact store is reported as an error, not faked."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "format": {"type": "string", "enum": _SUPPORTED_FORMATS},
                "title": {"type": "string"},
                "filename": {"type": "string", "description": "Optional file name; an extension is added if omitted."},
                "saveTo": {
                    "type": "string",
                    "description": "Where the readable copy goes: 'documents' (default), 'downloads', 'desktop', or an absolute folder path.",
                },
                "description": {"type": "string"},
                "tags": {"type": "array", "items": {"type": "string"}},
                "blocks": {
                    "type": "array",
                    "description": "Ordered content blocks for docx/markdown/txt.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "type": {
                                "type": "string",
                                "enum": ["heading", "paragraph", "bullets", "numbers", "table", "page_break"],
                            },
                            "text": {"type": "string"},
                            "level": {"type": "integer", "description": "Heading level 1-9."},
                            "items": {"type": "array", "items": {"type": "string"}},
                            "headers": {"type": "array", "items": {"type": "string"}},
                            "rows": {"type": "array", "items": {"type": "array"}},
                            "bold": {"type": "boolean"},
                            "italic": {"type": "boolean"},
                        },
                        "required": ["type"],
                    },
                },
                "sheets": {
                    "type": "array",
                    "description": "Worksheets for xlsx; the first row is a bolded header.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "rows": {"type": "array", "items": {"type": "array"}},
                        },
                    },
                },
                "slides": {
                    "type": "array",
                    "description": "Slides for pptx.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "title": {"type": "string"},
                            "bullets": {"type": "array", "items": {"type": "string"}},
                            "notes": {"type": "string"},
                        },
                    },
                },
                "rows": {
                    "type": "array",
                    "description": "Tabular rows for csv/json (arrays, or objects keyed by column).",
                    "items": {"type": ["array", "object"]},
                },
            },
            "required": ["format"],
        },
        handler=_create,
        permission="read",  # writes only into the artifact store + the export folder
        risk="low",
        timeout_ms=30_000,
    ))
    from .report_tools import register_report_tools  # noqa: PLC0415
    from .edit.tools import register_document_edit_tools  # noqa: PLC0415

    return [
        "document_create",
        *register_report_tools(registry),
        *register_document_edit_tools(registry),
        *register_file_locate_tool(registry),
    ]


__all__ = ["register_document_authoring_tools"]
