"""`file_locate` — find a file on the person's machine and answer with a path.

The chat filesystem tools are confined to the bound project, so "where is the
PDF you just made?" had no honest route when the file lived in Downloads or
Documents. This tool searches the folders a person actually uses and returns
ABSOLUTE paths, newest first. It is read-only, bounded, and says which folders
it searched — a miss is a fact ("not in these four folders"), never a guess.

A produced file is written to the same export folder by `documents.outputs`,
so "the PDF from a minute ago" is normally the first result.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..tools import ToolContext, ToolDefinition, ToolRegistry
from .outputs import locate, search_roots


def _size_label(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"


async def _locate(args: dict[str, Any], ctx: ToolContext) -> tuple[str, Any]:
    query = str(args.get("query") or args.get("name") or "").strip()
    folders = args.get("folders")
    if not folders:
        # The person's own folders, plus the bound project: a file the agent
        # wrote with the code tools lives there, not in Downloads.
        roots = search_roots()
        workspace = getattr(ctx, "workspace", None)
        root = getattr(workspace, "root", None)
        if root is not None:
            roots.append(Path(str(root)))
        folders = [str(root) for root in roots]
    result = locate(
        query,
        extensions=args.get("extensions"),
        since_days=args.get("sinceDays"),
        limit=int(args.get("limit") or 25),
        folders=folders,
    )
    files = result["files"]
    if not files:
        roots = ", ".join(result["roots"]) or "no searchable folder"
        text = (
            f"No file matching {query or 'any name'!r} was found. "
            f"I searched: {roots}. That is a miss in those folders, not proof the "
            "file does not exist — give me the folder name and I will look there."
        )
        return text, {**result, "count": 0}

    import datetime as _dt

    lines = [f"{len(files)} file(s), newest first:"]
    for item in files:
        stamp = _dt.datetime.fromtimestamp(item["modified"]).strftime("%Y-%m-%d %H:%M")
        lines.append(f"  {item['path']}  ({_size_label(item['size_bytes'])}, {stamp})")
    if result["truncated"]:
        lines.append("[more matches exist — narrow the name or add an extension]")
    return "\n".join(lines), {**result, "count": len(files)}


def register_file_locate_tool(registry: ToolRegistry) -> list[str]:
    """Register `file_locate` on the shared registry (idempotent)."""
    if registry.get("file_locate") is not None:
        return []
    registry.register(ToolDefinition(
        name="file_locate",
        description=(
            "Find a file the person already has (or one you just produced) and "
            "return its ABSOLUTE path. Searches the Eclipse export folder, "
            "Downloads, Documents and Desktop, newest first. Use it whenever the "
            "answer needs a real path — never say 'it is in your home folder'. "
            "Filter by name fragment or glob, extension and age."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Name fragment or glob, e.g. 'ulcer', 'report', '*.pdf'. Default '*'.",
                },
                "extensions": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional extensions without the dot, e.g. ['pdf','xlsx'].",
                },
                "sinceDays": {
                    "type": "number",
                    "description": "Only files modified in the last N days.",
                },
                "limit": {"type": "integer", "description": "Max results (1-200, default 25)."},
                "folders": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Absolute folders to search instead of the default set.",
                },
            },
        },
        handler=_locate,
        permission="read",
        risk="low",
        timeout_ms=30_000,
    ))
    return ["file_locate"]


__all__ = ["register_file_locate_tool", "search_roots"]
