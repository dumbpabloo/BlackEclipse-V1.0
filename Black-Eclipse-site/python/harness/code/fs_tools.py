"""The file-system tools (Phase FS) — the agent's file surface.

These tools ARE the FileSystemService exposed to the model. Each handler is thin:
it resolves the workspace, builds the service, calls ONE deterministic operation,
maps a structured `FsError` to a readable refusal, emits a content-free event and
returns `(text, data)` exactly like every other code tool.

Two workspaces, matching the existing code capability:

  * READ/INSPECT/METADATA/INDEX/WATCH tools read the server-resolved root
    (`ctx.workspace`) — the same read-only navigator surface as `workspace_read`.
  * WRITE/EDIT tools use the disposable code workspace (`_ensure_code_workspace`),
    so a change lands in the task's own copy (or the user's real folder in direct
    mode), never silently in the checkout.

Every write path is gated three times: the autonomy allowlist (structural), the
effect authorizer (`make_code_authorizer` admits fs writes), and the handler →
`permissions.require` → `policy.decide_write` per path. Ambiguous and stale edits
are always refused with a recovery hint; multi-file changes are atomic.
"""

from __future__ import annotations

import time
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field

from ..tools import ToolContext, ToolDefinition, ToolRegistry
from .events import diff_event_payload
from .fs.errors import WORKSPACE_UNBOUND, FsError
from .fs.intent import is_insert_before, parse_edit_instruction
from .fs.path_hints import with_path_suggestions
from .media import maybe_read_media
from .fs.post_summary import summarize as _summary
from .fs.service import FileSystemService
from .tools import _ensure_code_workspace, _make_actor, _present, emit_code_event

MAX_PATH = 1024
MAX_CONTENT = 1_000_000
MAX_PATCHES = 50


# ── input models ────────────────────────────────────────────────────────────


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ListDirectoryInput(_Base):
    path: str = "."
    recursive: bool = False
    maxDepth: int = Field(1, ge=1, le=12)  # noqa: N815
    limit: int = Field(500, ge=1, le=5000)
    offset: int = Field(0, ge=0)
    includeHidden: bool = False  # noqa: N815
    includeIgnored: bool = False  # noqa: N815
    pattern: Optional[str] = Field(None, max_length=200)
    directoriesOnly: bool = False  # noqa: N815
    filesOnly: bool = False  # noqa: N815
    sort: str = Field("name", pattern="^(name|modified|size)$")
    includeSizes: bool = False  # noqa: N815


class ReadFileInput(_Base):
    path: str = Field(..., min_length=1, max_length=MAX_PATH)
    startLine: Optional[int] = Field(None, ge=1)  # noqa: N815
    endLine: Optional[int] = Field(None, ge=1)  # noqa: N815
    offset: Optional[int] = Field(None, ge=0)
    limit: Optional[int] = Field(None, ge=1, le=2000)
    encoding: Optional[str] = Field(None, max_length=32)
    lineNumbers: bool = True  # noqa: N815


class WriteFileInput(_Base):
    path: str = Field(..., min_length=1, max_length=MAX_PATH)
    content: str = Field("", max_length=MAX_CONTENT)
    encoding: Optional[str] = Field(None, max_length=32)
    createIfMissing: bool = True  # noqa: N815
    overwrite: bool = True
    expectedHash: Optional[str] = Field(None, max_length=128)  # noqa: N815
    dryRun: bool = False  # noqa: N815


class CreateFileInput(_Base):
    path: str = Field(..., min_length=1, max_length=MAX_PATH)
    content: str = Field("", max_length=MAX_CONTENT)
    overwrite: bool = False
    encoding: Optional[str] = Field(None, max_length=32)
    dryRun: bool = False  # noqa: N815


class DeleteFileInput(_Base):
    path: str = Field(..., min_length=1, max_length=MAX_PATH)
    expectedHash: Optional[str] = Field(None, max_length=128)  # noqa: N815
    dryRun: bool = False  # noqa: N815


class MoveFileInput(_Base):
    source: str = Field(..., min_length=1, max_length=MAX_PATH)
    destination: str = Field(..., min_length=1, max_length=MAX_PATH)
    overwrite: bool = False
    expectedHash: Optional[str] = Field(None, max_length=128)  # noqa: N815
    dryRun: bool = False  # noqa: N815


class CopyFileInput(_Base):
    source: str = Field(..., min_length=1, max_length=MAX_PATH)
    destination: str = Field(..., min_length=1, max_length=MAX_PATH)
    overwrite: bool = False
    recursive: bool = True
    dryRun: bool = False  # noqa: N815


class RenameFileInput(_Base):
    path: str = Field(..., min_length=1, max_length=MAX_PATH)
    newName: str = Field(..., min_length=1, max_length=255)  # noqa: N815
    overwrite: bool = False
    dryRun: bool = False  # noqa: N815


class CreateDirectoryInput(_Base):
    path: str = Field(..., min_length=1, max_length=MAX_PATH)
    parents: bool = True
    existOk: bool = False  # noqa: N815
    dryRun: bool = False  # noqa: N815


class DeleteDirectoryInput(_Base):
    path: str = Field(..., min_length=1, max_length=MAX_PATH)
    recursive: bool = False
    dryRun: bool = False  # noqa: N815


class SearchTextInput(_Base):
    query: str = Field(..., min_length=1, max_length=500)
    path: str = "."
    regex: bool = False
    caseSensitive: bool = False  # noqa: N815
    wholeWord: bool = False  # noqa: N815
    glob: Optional[str] = Field(None, max_length=200)
    maxResults: int = Field(100, ge=1, le=500)  # noqa: N815
    contextLines: int = Field(0, ge=0, le=20)  # noqa: N815
    includeIgnored: bool = False  # noqa: N815
    includeHidden: bool = False  # noqa: N815


class SearchFilesInput(_Base):
    pattern: str = Field(..., min_length=1, max_length=200)
    exclude: Optional[list[str]] = Field(None, max_length=32)
    extensions: Optional[list[str]] = Field(None, max_length=32)
    path: str = "."
    directoriesOnly: bool = False  # noqa: N815
    filesOnly: bool = False  # noqa: N815
    includeHidden: bool = False  # noqa: N815
    includeIgnored: bool = False  # noqa: N815
    limit: int = Field(500, ge=1, le=500)


class FindFilesInput(_Base):
    include: Optional[list[str]] = Field(None, max_length=32)
    exclude: Optional[list[str]] = Field(None, max_length=32)
    extensions: Optional[list[str]] = Field(None, max_length=32)
    path: str = "."
    directoriesOnly: bool = False  # noqa: N815
    filesOnly: bool = False  # noqa: N815
    includeHidden: bool = False  # noqa: N815
    includeIgnored: bool = False  # noqa: N815
    limit: int = Field(500, ge=1, le=500)


class SearchSymbolsInput(_Base):
    query: str = Field(..., min_length=1, max_length=200)
    path: str = "."
    kinds: Optional[list[str]] = Field(None, max_length=12)
    language: Optional[str] = Field(None, max_length=32)
    regex: bool = False
    caseSensitive: bool = False  # noqa: N815
    maxResults: int = Field(100, ge=1, le=500)  # noqa: N815


class FindReferencesInput(_Base):
    symbol: str = Field(..., min_length=1, max_length=200)
    path: str = "."
    glob: Optional[str] = Field(None, max_length=200)
    maxResults: int = Field(200, ge=1, le=500)  # noqa: N815


class PatchFileInput(_Base):
    path: str = Field(..., min_length=1, max_length=MAX_PATH)
    operation: str = Field("replace", pattern="^(replace|insert|delete|append|prepend)$")
    oldText: Optional[str] = Field(None, max_length=MAX_CONTENT)  # noqa: N815
    newText: Optional[str] = Field(None, max_length=MAX_CONTENT)  # noqa: N815
    expectedHash: Optional[str] = Field(None, max_length=128)  # noqa: N815
    dryRun: bool = False  # noqa: N815


class ReplaceTextInput(_Base):
    path: str = Field(..., min_length=1, max_length=MAX_PATH)
    oldText: str = Field(..., min_length=1, max_length=MAX_CONTENT)  # noqa: N815
    newText: str = Field(..., max_length=MAX_CONTENT)  # noqa: N815
    count: int = Field(1, ge=0, le=1000)
    expectedOccurrences: Optional[int] = Field(None, ge=0, le=1000)  # noqa: N815
    regex: bool = False
    caseSensitive: bool = True  # noqa: N815
    expectedHash: Optional[str] = Field(None, max_length=128)  # noqa: N815
    dryRun: bool = False  # noqa: N815


class InsertTextInput(_Base):
    path: str = Field(..., min_length=1, max_length=MAX_PATH)
    text: str = Field(..., max_length=MAX_CONTENT)
    anchor: Optional[str] = Field(None, max_length=MAX_CONTENT)
    afterAnchor: Optional[str] = Field(None, max_length=MAX_CONTENT)  # noqa: N815
    beforeAnchor: Optional[str] = Field(None, max_length=MAX_CONTENT)  # noqa: N815
    position: str = Field("after", pattern="^(after|before|start|end)$")
    atLine: Optional[int] = Field(None, ge=1)  # noqa: N815
    atOffset: Optional[int] = Field(None, ge=0)  # noqa: N815
    expectedHash: Optional[str] = Field(None, max_length=128)  # noqa: N815
    dryRun: bool = False  # noqa: N815


class DeleteTextInput(_Base):
    path: str = Field(..., min_length=1, max_length=MAX_PATH)
    oldText: Optional[str] = Field(None, max_length=MAX_CONTENT)  # noqa: N815
    startLine: Optional[int] = Field(None, ge=1)  # noqa: N815
    endLine: Optional[int] = Field(None, ge=1)  # noqa: N815
    expectedHash: Optional[str] = Field(None, max_length=128)  # noqa: N815
    dryRun: bool = False  # noqa: N815


class ApplyDiffInput(_Base):
    diff: str = Field(..., min_length=1, max_length=2_000_000)
    dryRun: bool = False  # noqa: N815


class MultiFilePatchInput(_Base):
    patches: list[dict[str, Any]] = Field(..., min_length=1, max_length=MAX_PATCHES)
    atomic: bool = True
    dryRun: bool = False  # noqa: N815


class FileMetadataInput(_Base):
    path: str = Field(..., min_length=1, max_length=MAX_PATH)


class FileHashInput(_Base):
    path: str = Field(..., min_length=1, max_length=MAX_PATH)


class FilePermissionsInput(_Base):
    path: str = Field(..., min_length=1, max_length=MAX_PATH)


class IndexWorkspaceInput(_Base):
    force: bool = False


class WatchPathInput(_Base):
    path: str = "."
    recursive: bool = True


class EditFileInput(_Base):
    path: str = Field(..., min_length=1, max_length=MAX_PATH)
    instruction: str = Field(..., min_length=1, max_length=2000)
    query: Optional[str] = Field(None, max_length=200)
    oldText: Optional[str] = Field(None, max_length=MAX_CONTENT)  # noqa: N815
    newText: Optional[str] = Field(None, max_length=MAX_CONTENT)  # noqa: N815
    expectedHash: Optional[str] = Field(None, max_length=128)  # noqa: N815
    dryRun: bool = False  # noqa: N815


class VerifyFileInput(_Base):
    path: str = Field(..., min_length=1, max_length=MAX_PATH)
    expectedHash: Optional[str] = Field(None, max_length=128)  # noqa: N815


class ValidateChangesInput(_Base):
    edits: list[dict[str, Any]] = Field(..., min_length=1, max_length=MAX_PATCHES)


class GitStatusInput(_Base):
    pass


# ── JSON schemas (strict; the registry builds its arg model from these) ──────


def _obj(properties: dict[str, Any], required: Optional[list[str]] = None) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": "object", "properties": properties, "additionalProperties": False}
    if required:
        schema["required"] = required
    return schema


_STR = {"type": "string"}
_INT = {"type": "integer"}
_BOOL = {"type": "boolean"}

_INPUT_SCHEMAS: dict[str, dict[str, Any]] = {
    "list_directory": _obj({
        "path": {**_STR, "description": "Folder to list, relative to the project."},
        "recursive": {**_BOOL, "description": "Descend into subfolders."},
        "maxDepth": {**_INT, "description": "How deep to descend (default 1)."},
        "limit": {**_INT, "description": "Maximum entries to return (default 500)."},
        "offset": {**_INT, "description": "Skip this many entries (paging)."},
        "includeHidden": {**_BOOL, "description": "Include dotfiles."},
        "includeIgnored": {**_BOOL, "description": "Include generated/ignored paths."},
        "pattern": {**_STR, "description": "Glob filter on names (e.g. *.ts)."},
        "directoriesOnly": {**_BOOL, "description": "Only folders."},
        "filesOnly": {**_BOOL, "description": "Only files."},
        "sort": {**_STR, "enum": ["name", "modified", "size"]},
        "includeSizes": {**_BOOL, "description": "Include MIME type per file."},
    }, ["path"]),
    "read_file": _obj({
        "path": {**_STR, "description": "File to read, relative to the project."},
        "startLine": {**_INT, "description": "First line (1-based)."},
        "endLine": {**_INT, "description": "Last line (1-based, inclusive)."},
        "offset": {**_INT, "description": "First line (0-based) when startLine is absent."},
        "limit": {**_INT, "description": "Maximum lines to return."},
        "encoding": {**_STR, "description": "Force an encoding (default: detect)."},
        "lineNumbers": {**_BOOL, "description": "Prefix each line with its number (default true)."},
    }, ["path"]),
    "write_file": _obj({
        "path": {**_STR}, "content": {**_STR},
        "encoding": {**_STR, "description": "Force an encoding (default: the file's own)."},
        "createIfMissing": {**_BOOL}, "overwrite": {**_BOOL},
        "expectedHash": {**_STR, "description": "Refuse if the file's hash differs (stale check)."},
        "dryRun": {**_BOOL, "description": "Compute the change but write nothing."},
    }, ["path"]),
    "create_file": _obj({
        "path": {**_STR}, "content": {**_STR}, "overwrite": {**_BOOL},
        "encoding": {**_STR}, "dryRun": {**_BOOL},
    }, ["path"]),
    "delete_file": _obj({
        "path": {**_STR}, "expectedHash": {**_STR}, "dryRun": {**_BOOL},
    }, ["path"]),
    "move_file": _obj({
        "source": {**_STR}, "destination": {**_STR}, "overwrite": {**_BOOL},
        "expectedHash": {**_STR}, "dryRun": {**_BOOL},
    }, ["source", "destination"]),
    "copy_file": _obj({
        "source": {**_STR}, "destination": {**_STR}, "overwrite": {**_BOOL},
        "recursive": {**_BOOL}, "dryRun": {**_BOOL},
    }, ["source", "destination"]),
    "rename_file": _obj({
        "path": {**_STR}, "newName": {**_STR}, "overwrite": {**_BOOL}, "dryRun": {**_BOOL},
    }, ["path", "newName"]),
    "create_directory": _obj({
        "path": {**_STR}, "parents": {**_BOOL}, "existOk": {**_BOOL}, "dryRun": {**_BOOL},
    }, ["path"]),
    "delete_directory": _obj({
        "path": {**_STR}, "recursive": {**_BOOL, "description": "Required to remove a non-empty folder."}, "dryRun": {**_BOOL},
    }, ["path"]),
    "search_text": _obj({
        "query": {**_STR, "description": "Literal text to find (regex=true for a pattern/alternation)."},
        "path": {**_STR, "description": "Folder, or a single file, to search (default '.')"},
        "regex": {**_BOOL}, "caseSensitive": {**_BOOL}, "wholeWord": {**_BOOL},
        "glob": {**_STR}, "maxResults": {**_INT},
        "contextLines": {**_INT, "description": "Lines of context around each match (0-20, default 0)."},
        "includeIgnored": {**_BOOL}, "includeHidden": {**_BOOL},
    }, ["query"]),
    "search_files": _obj({
        "pattern": {**_STR, "description": "Glob on file names/paths (e.g. **/*.tsx)."},
        "exclude": {"type": "array", "items": _STR}, "extensions": {"type": "array", "items": _STR},
        "directoriesOnly": {**_BOOL}, "filesOnly": {**_BOOL},
        "includeHidden": {**_BOOL}, "includeIgnored": {**_BOOL},
        "path": {**_STR}, "limit": {**_INT},
    }, ["pattern"]),
    "find_files": _obj({
        "include": {"type": "array", "items": _STR}, "exclude": {"type": "array", "items": _STR},
        "extensions": {"type": "array", "items": _STR}, "path": {**_STR},
        "directoriesOnly": {**_BOOL}, "filesOnly": {**_BOOL},
        "includeHidden": {**_BOOL}, "includeIgnored": {**_BOOL}, "limit": {**_INT},
    }),
    "search_symbols": _obj({
        "query": {**_STR, "description": "Symbol name (or regex) to find."},
        "path": {**_STR},
        "kinds": {"type": "array", "items": _STR, "description": "function/class/interface/type/variable/constant/export/import"},
        "language": {**_STR}, "regex": {**_BOOL}, "caseSensitive": {**_BOOL}, "maxResults": {**_INT},
    }, ["query"]),
    "find_references": _obj({
        "symbol": {**_STR}, "path": {**_STR}, "glob": {**_STR}, "maxResults": {**_INT},
    }, ["symbol"]),
    "patch_file": _obj({
        "path": {**_STR},
        "operation": {**_STR, "enum": ["replace", "insert", "delete", "append", "prepend"]},
        "oldText": {**_STR}, "newText": {**_STR},
        "expectedHash": {**_STR}, "dryRun": {**_BOOL},
    }, ["path", "operation"]),
    "replace_text": _obj({
        "path": {**_STR}, "oldText": {**_STR}, "newText": {**_STR},
        "count": {**_INT, "description": "How many occurrences to replace (default 1)."},
        "expectedOccurrences": {**_INT, "description": "Require exactly this many; refuse otherwise."},
        "regex": {**_BOOL}, "caseSensitive": {**_BOOL},
        "expectedHash": {**_STR}, "dryRun": {**_BOOL},
    }, ["path", "oldText", "newText"]),
    "insert_text": _obj({
        "path": {**_STR}, "text": {**_STR},
        "anchor": {**_STR}, "afterAnchor": {**_STR}, "beforeAnchor": {**_STR},
        "position": {**_STR, "enum": ["after", "before", "start", "end"]},
        "atLine": {**_INT}, "atOffset": {**_INT},
        "expectedHash": {**_STR}, "dryRun": {**_BOOL},
    }, ["path", "text"]),
    "delete_text": _obj({
        "path": {**_STR}, "oldText": {**_STR}, "startLine": {**_INT}, "endLine": {**_INT},
        "expectedHash": {**_STR}, "dryRun": {**_BOOL},
    }, ["path"]),
    "apply_diff": _obj({
        "diff": {**_STR, "description": "A standard unified diff."}, "dryRun": {**_BOOL},
    }, ["diff"]),
    "multi_file_patch": _obj({
        "patches": {"type": "array", "items": {"type": "object"}, "description": "Ordered patches; each is {path, operation, oldText, newText, ...}."},
        "atomic": {**_BOOL, "description": "Validate all before applying any (default true)."},
        "dryRun": {**_BOOL},
    }, ["patches"]),
    "file_metadata": _obj({"path": _STR}, ["path"]),
    "file_hash": _obj({"path": _STR}, ["path"]),
    "file_permissions": _obj({"path": _STR}, ["path"]),
    "index_workspace": _obj({"force": {**_BOOL, "description": "Rebuild the index from scratch."}}),
    "watch_file": _obj({"path": _STR, "recursive": _BOOL}, ["path"]),
    "watch_directory": _obj({"path": _STR, "recursive": _BOOL}, ["path"]),
    "edit_file": _obj({
        "path": {**_STR},
        "instruction": {**_STR, "description": "What to change, in plain language."},
        "query": {**_STR, "description": "Optional text to locate the region."},
        "oldText": {**_STR}, "newText": {**_STR},
        "expectedHash": {**_STR}, "dryRun": {**_BOOL},
    }, ["path", "instruction"]),
    "verify_file": _obj({
        "path": {**_STR}, "expectedHash": {**_STR, "description": "Optional hash the file should have."},
    }, ["path"]),
    "validate_changes": _obj({
        "edits": {"type": "array", "items": {"type": "object"},
                  "description": "Proposed edits: {path, operation, oldText?, newText?, expectedHash?}."},
    }, ["edits"]),
    "git_status": _obj({}),
}


# ── service resolution ──────────────────────────────────────────────────────


def _fs_config(ctx: ToolContext) -> dict[str, Any]:
    raw = getattr(ctx, "fs_permissions", None)
    return raw if isinstance(raw, dict) else {}


_READ_CACHE = "_fs_read_service"
_WRITE_CACHE = "_fs_write_service"


def _read_service(ctx: ToolContext) -> FileSystemService:
    existing = getattr(ctx, _READ_CACHE, None)
    if existing is not None:
        return existing
    ws = getattr(ctx, "workspace", None)
    if ws is None:
        raise FsError(WORKSPACE_UNBOUND, "There is no project folder to work in.")
    actor = _make_actor(ws.id, getattr(ctx, "autonomy", None))
    service = FileSystemService(
        ws, ws.binding, actor, config=_fs_config(ctx),
        run_id=getattr(ctx, "run_id", None),
    )
    try:
        setattr(ctx, _READ_CACHE, service)
    except Exception:  # noqa: BLE001 - a frozen ctx must not break the call
        pass
    return service


def _write_service(ctx: ToolContext) -> FileSystemService:
    existing = getattr(ctx, _WRITE_CACHE, None)
    if existing is not None:
        return existing
    if getattr(ctx, "workspace", None) is None:
        # No workspace binding ⇒ no code capability (the Phase-0 rule). Without
        # this guard a write would allocate a workspace against the built-in repo.
        raise FsError(WORKSPACE_UNBOUND, "There is no project folder to work in.")
    _cw, binding, resolved, actor = _ensure_code_workspace(ctx)
    service = FileSystemService(
        resolved, binding, actor, config=_fs_config(ctx),
        run_id=getattr(ctx, "run_id", None),
        turn_key=getattr(ctx, "checkpoint_turn", None),
    )
    try:
        setattr(ctx, _WRITE_CACHE, service)
    except Exception:  # noqa: BLE001
        pass
    return service


async def _emit(ctx: ToolContext, event_type: str, data: dict[str, Any]) -> None:
    await emit_code_event(ctx, event_type, data)


# A single change event carries at most this many per-file previews; a larger
# multi-file change is still fully applied — the event just shows the first N.
_MAX_EVENT_PREVIEW_FILES = 8


def _event_previews(data: Any) -> list[dict[str, Any]]:
    """Bounded per-file change records for the live event stream, or [].

    Extracts the diff previews the fs operations ALREADY compute (a single-file
    `diff`/`additions`/`deletions` record, or the `previews`/`results` list a
    multi-file change returns) so the chat and workspace can show WHAT changed.
    Never raises, never fabricates: a result with no diff yields [].
    """
    if not isinstance(data, dict) or data.get("dryRun") is True:
        return []
    records: list[dict[str, Any]] = []
    for key in ("previews", "results", "changes"):
        value = data.get(key)
        if isinstance(value, list) and value:
            records = [v for v in value if isinstance(v, dict)]
            break
    if not records and isinstance(data.get("diff"), str):
        records = [data]
    out: list[dict[str, Any]] = []
    for record in records[:_MAX_EVENT_PREVIEW_FILES]:
        if record.get("changed") is False:
            # An explicit no-op ("the file already says that") is not a change.
            continue
        path = record.get("path")
        if not isinstance(path, str) or not path:
            continue
        entry: dict[str, Any] = {
            "path": path,
            "op": str(record.get("op") or record.get("operation") or "modify"),
        }
        payload = diff_event_payload(record)
        if payload:
            entry.update(payload)
        out.append(entry)
    if not out and data.get("moved") is True and isinstance(data.get("to"), str):
        out.append({"path": data["to"], "op": "move", "from": data.get("from")})
    elif not out and data.get("copied") is True and isinstance(data.get("to"), str):
        out.append({"path": data["to"], "op": "copy", "from": data.get("from")})
    return out


def _refusal_text(error: FsError) -> str:
    text = f"REFUSED ({error.code}): {error.reason}"
    if error.recovery:
        text += f" {error.recovery}"
    return text


def _ok(verb: str, path: str, data: dict[str, Any]) -> tuple[str, Any]:
    return _summary(data, verb=verb, path=path), data


async def _dispatch(
    ctx: ToolContext,
    tool: str,
    *,
    kind: str,
    path: str,
    run: Any,
    verb: str,
    transactional: bool = False,
) -> tuple[str, Any]:
    """Resolve the service, run the operation, emit an event, map errors."""
    started = time.monotonic()
    service: Optional[FileSystemService] = None
    if transactional:
        await _emit(ctx, "fs.transaction.started", {"tool": tool, "path": path})
    try:
        service = _write_service(ctx) if kind == "write" else _read_service(ctx)
        data = run(service)
    except FsError as exc:
        if transactional:
            await _emit(ctx, "fs.transaction.rolled_back", {"tool": tool, "code": exc.code})
        await _emit(ctx, f"fs.{tool}.refused", {
            "tool": tool, "code": exc.code, "path": path or exc.path,
            "workspaceId": getattr(getattr(service, "workspace", None), "id", ""),
            "durationMs": int((time.monotonic() - started) * 1000),
        })
        return _refusal_text(exc), exc.to_dict()
    except Exception as exc:  # noqa: BLE001 - a tool failure is data, not a crash
        if transactional:
            await _emit(ctx, "fs.transaction.rolled_back", {"tool": tool, "code": type(exc).__name__})
        await _emit(ctx, f"fs.{tool}.failed", {"tool": tool, "error": type(exc).__name__})
        raise
    if transactional:
        await _emit(ctx, "fs.transaction.committed", {
            "tool": tool, "filesChanged": data.get("filesChanged", 1),
        })
    previews = _event_previews(data)
    await _emit(ctx, f"fs.{tool}", {
        "tool": tool, "path": data.get("path", path),
        "workspaceId": getattr(getattr(service, "workspace", None), "id", ""),
        "dryRun": bool(data.get("dryRun")),
        "filesChanged": data.get("filesChanged", 1),
        "bytesWritten": data.get("bytesWritten", 0),
        "durationMs": int((time.monotonic() - started) * 1000),
        **({"previews": previews} if previews else {}),
    })
    return _ok(verb, data.get("path", path) or path, data)


# ── handlers ────────────────────────────────────────────────────────────────


async def _list_directory(args: dict[str, Any], ctx: ToolContext) -> tuple[str, Any]:
    model = ListDirectoryInput(**_present(args))
    data = await _dispatch(
        ctx, "list_directory", kind="read", path=model.path, verb="Listed",
        run=lambda svc: svc.list_directory(
            model.path, recursive=model.recursive, max_depth=model.maxDepth,
            limit=model.limit, offset=model.offset, include_hidden=model.includeHidden,
            include_ignored=model.includeIgnored, pattern=model.pattern,
            directories_only=model.directoriesOnly, files_only=model.filesOnly,
            sort=model.sort, include_sizes=model.includeSizes,
        ),
    )
    text, payload = data
    entries = payload.get("entries") if isinstance(payload, dict) else None
    if entries is not None:
        lines = [f"{payload.get('count', 0)} of {payload.get('totalCount', 0)} entr(ies) in {model.path}:"]
        for entry in entries[:200]:
            lines.append(f"  {entry['path']}")
        text = "\n".join(lines)
    return text, payload


async def _read_file(args: dict[str, Any], ctx: ToolContext) -> tuple[str, Any]:
    model = ReadFileInput(**_present(args))
    media = maybe_read_media(_read_service(ctx), model.path)
    if media is not None:
        return media
    text, payload = await _dispatch(
        ctx, "read_file", kind="read", path=model.path, verb="Read",
        run=lambda svc: svc.read_file(
            model.path, start_line=model.startLine, end_line=model.endLine,
            offset=model.offset, limit=model.limit, encoding=model.encoding,
            line_numbers=model.lineNumbers,
        ),
    )
    text, payload = with_path_suggestions(ctx, model.path, text, payload)
    if isinstance(payload, dict) and payload.get("status") == "ok":
        header = f"{payload['path']} (lines {payload['startLine']}-{payload['endLine']} of {payload['totalLines']})"
        body = [header, *payload["content"]]
        if payload.get("nextStartLine"):
            body.append(f"[continues — call read_file with startLine={payload['nextStartLine']}]")
        text = "\n".join(body)
    return text, payload


async def _write_file(args: dict[str, Any], ctx: ToolContext) -> tuple[str, Any]:
    model = WriteFileInput(**_present(args))
    return await _dispatch(
        ctx, "write_file", kind="write", path=model.path, verb="Wrote",
        run=lambda svc: svc.write_file(
            model.path, model.content, encoding=model.encoding,
            create_if_missing=model.createIfMissing, overwrite=model.overwrite,
            expected_hash=model.expectedHash, dry_run=model.dryRun,
        ),
    )


async def _create_file(args: dict[str, Any], ctx: ToolContext) -> tuple[str, Any]:
    model = CreateFileInput(**_present(args))
    return await _dispatch(
        ctx, "create_file", kind="write", path=model.path, verb="Created",
        run=lambda svc: svc.create_file(
            model.path, model.content, overwrite=model.overwrite,
            encoding=model.encoding, dry_run=model.dryRun,
        ),
    )


async def _delete_file(args: dict[str, Any], ctx: ToolContext) -> tuple[str, Any]:
    model = DeleteFileInput(**_present(args))
    return await _dispatch(
        ctx, "delete_file", kind="write", path=model.path, verb="Deleted",
        run=lambda svc: svc.delete_file(model.path, expected_hash=model.expectedHash, dry_run=model.dryRun),
    )


async def _move_file(args: dict[str, Any], ctx: ToolContext) -> tuple[str, Any]:
    model = MoveFileInput(**_present(args))
    return await _dispatch(
        ctx, "move_file", kind="write", path=model.source, verb="Moved",
        run=lambda svc: svc.move_file(
            model.source, model.destination, overwrite=model.overwrite,
            expected_hash=model.expectedHash, dry_run=model.dryRun,
        ),
    )


async def _copy_file(args: dict[str, Any], ctx: ToolContext) -> tuple[str, Any]:
    model = CopyFileInput(**_present(args))
    return await _dispatch(
        ctx, "copy_file", kind="write", path=model.source, verb="Copied",
        run=lambda svc: svc.copy_file(
            model.source, model.destination, overwrite=model.overwrite,
            recursive=model.recursive, dry_run=model.dryRun,
        ),
    )


async def _rename_file(args: dict[str, Any], ctx: ToolContext) -> tuple[str, Any]:
    model = RenameFileInput(**_present(args))
    return await _dispatch(
        ctx, "rename_file", kind="write", path=model.path, verb="Renamed",
        run=lambda svc: svc.rename_file(model.path, model.newName, overwrite=model.overwrite, dry_run=model.dryRun),
    )


async def _create_directory(args: dict[str, Any], ctx: ToolContext) -> tuple[str, Any]:
    model = CreateDirectoryInput(**_present(args))
    return await _dispatch(
        ctx, "create_directory", kind="write", path=model.path, verb="Created folder",
        run=lambda svc: svc.create_directory(
            model.path, parents=model.parents, exist_ok=model.existOk, dry_run=model.dryRun,
        ),
    )


async def _delete_directory(args: dict[str, Any], ctx: ToolContext) -> tuple[str, Any]:
    model = DeleteDirectoryInput(**_present(args))
    return await _dispatch(
        ctx, "delete_directory", kind="write", path=model.path, verb="Removed folder",
        run=lambda svc: svc.delete_directory(model.path, recursive=model.recursive, dry_run=model.dryRun),
    )


async def _search_text(args: dict[str, Any], ctx: ToolContext) -> tuple[str, Any]:
    model = SearchTextInput(**_present(args))
    text, payload = await _dispatch(
        ctx, "search_text", kind="read", path=model.path, verb="Searched",
        run=lambda svc: svc.search_text(
            model.query, path=model.path, regex=model.regex, case_sensitive=model.caseSensitive,
            whole_word=model.wholeWord, glob=model.glob, max_results=model.maxResults,
            context_lines=model.contextLines, include_ignored=model.includeIgnored,
            include_hidden=model.includeHidden,
        ),
    )
    if isinstance(payload, dict) and payload.get("status") == "ok":
        # Name the match mode in the summary: a LITERAL miss must never read as
        # "this symbol/code does not exist" (alternation queries silently match
        # nothing unless regex=true).
        mode = "regex" if model.regex else "literal text"
        lines = [f"{payload['count']} match(es) for {model.query!r} ({mode} match; engine: {payload.get('engine')}):"]
        for match in payload["matches"][:200]:
            lines.append(f"  {match['path']}:{match['line']}:{match['column']}: {match['text'].strip()}")
        if payload.get("truncated"):
            lines.append("[truncated]")
        if payload["count"] == 0 and not model.regex:
            lines.append(
                "0 matches means this exact literal text was not found — it does NOT prove the "
                "symbol/code is absent. For alternation (`a|b`) or any pattern, retry with regex: true."
            )
        text = "\n".join(lines)
    return text, payload


async def _search_files(args: dict[str, Any], ctx: ToolContext) -> tuple[str, Any]:
    model = SearchFilesInput(**_present(args))
    text, payload = await _dispatch(
        ctx, "search_files", kind="read", path=model.path, verb="Searched files",
        run=lambda svc: svc.search_files(
            model.pattern, exclude=model.exclude, extensions=model.extensions,
            path=model.path, directories_only=model.directoriesOnly,
            files_only=model.filesOnly, include_hidden=model.includeHidden,
            include_ignored=model.includeIgnored, limit=model.limit,
        ),
    )
    if isinstance(payload, dict) and payload.get("status") == "ok":
        lines = [f"{payload['count']} file(s) matching {model.pattern!r}:"]
        for entry in payload["files"][:200]:
            lines.append(f"  {entry['path']}")
        text = "\n".join(lines)
    return text, payload


async def _find_files(args: dict[str, Any], ctx: ToolContext) -> tuple[str, Any]:
    model = FindFilesInput(**_present(args))
    text, payload = await _dispatch(
        ctx, "find_files", kind="read", path=model.path, verb="Found files",
        run=lambda svc: svc.find_files(
            include=model.include, exclude=model.exclude, extensions=model.extensions,
            path=model.path, directories_only=model.directoriesOnly, files_only=model.filesOnly,
            include_hidden=model.includeHidden, include_ignored=model.includeIgnored, limit=model.limit,
        ),
    )
    if isinstance(payload, dict) and payload.get("status") == "ok":
        lines = [f"{payload['count']} file(s):"]
        for entry in payload["files"][:200]:
            lines.append(f"  {entry['path']}")
        text = "\n".join(lines)
    return text, payload


async def _search_symbols(args: dict[str, Any], ctx: ToolContext) -> tuple[str, Any]:
    model = SearchSymbolsInput(**_present(args))
    text, payload = await _dispatch(
        ctx, "search_symbols", kind="read", path=model.path, verb="Searched symbols",
        run=lambda svc: svc.search_symbols(
            model.query, path=model.path, kinds=model.kinds, language=model.language,
            regex=model.regex, case_sensitive=model.caseSensitive, max_results=model.maxResults,
        ),
    )
    if isinstance(payload, dict) and payload.get("status") == "ok":
        lines = [f"{payload['count']} symbol(s) matching {model.query!r} (engine: {payload.get('engine')}):"]
        for symbol in payload["symbols"][:200]:
            lines.append(f"  {symbol['kind']} {symbol['name']} — {symbol['path']}:{symbol['line']}")
        text = "\n".join(lines)
    return text, payload


async def _find_references(args: dict[str, Any], ctx: ToolContext) -> tuple[str, Any]:
    model = FindReferencesInput(**_present(args))
    text, payload = await _dispatch(
        ctx, "find_references", kind="read", path=model.path, verb="Found references",
        run=lambda svc: svc.find_references(
            model.symbol, path=model.path, glob=model.glob, max_results=model.maxResults,
        ),
    )
    if isinstance(payload, dict) and payload.get("status") == "ok":
        lines = [f"{payload['count']} reference(s) to {model.symbol!r} (engine: {payload.get('engine')}):"]
        for ref in payload["references"][:200]:
            marker = " [definition]" if ref.get("definition") else ""
            lines.append(f"  {ref['path']}:{ref['line']}{marker}: {ref.get('text', '')}")
        text = "\n".join(lines)
    return text, payload


async def _patch_file(args: dict[str, Any], ctx: ToolContext) -> tuple[str, Any]:
    model = PatchFileInput(**_present(args))
    return await _dispatch(
        ctx, "patch_file", kind="write", path=model.path, verb="Patched",
        run=lambda svc: svc.patch_file(
            model.path, model.operation, old_text=model.oldText, new_text=model.newText,
            expected_hash=model.expectedHash, dry_run=model.dryRun,
        ),
    )


async def _replace_text(args: dict[str, Any], ctx: ToolContext) -> tuple[str, Any]:
    model = ReplaceTextInput(**_present(args))
    return await _dispatch(
        ctx, "replace_text", kind="write", path=model.path, verb="Replaced",
        run=lambda svc: svc.replace_text(
            model.path, model.oldText, model.newText, count=model.count,
            expected_occurrences=model.expectedOccurrences, regex=model.regex,
            case_sensitive=model.caseSensitive, expected_hash=model.expectedHash, dry_run=model.dryRun,
        ),
    )


async def _insert_text(args: dict[str, Any], ctx: ToolContext) -> tuple[str, Any]:
    model = InsertTextInput(**_present(args))
    return await _dispatch(
        ctx, "insert_text", kind="write", path=model.path, verb="Inserted",
        run=lambda svc: svc.insert_text(
            model.path, model.text, anchor=model.anchor, after_anchor=model.afterAnchor,
            before_anchor=model.beforeAnchor, position=model.position, at_line=model.atLine,
            at_offset=model.atOffset, expected_hash=model.expectedHash, dry_run=model.dryRun,
        ),
    )


async def _delete_text(args: dict[str, Any], ctx: ToolContext) -> tuple[str, Any]:
    model = DeleteTextInput(**_present(args))
    return await _dispatch(
        ctx, "delete_text", kind="write", path=model.path, verb="Deleted text",
        run=lambda svc: svc.delete_text(
            model.path, old_text=model.oldText, start_line=model.startLine,
            end_line=model.endLine, expected_hash=model.expectedHash, dry_run=model.dryRun,
        ),
    )


async def _apply_diff(args: dict[str, Any], ctx: ToolContext) -> tuple[str, Any]:
    model = ApplyDiffInput(**_present(args))
    return await _dispatch(
        ctx, "apply_diff", kind="write", path="", verb="Applied diff",
        run=lambda svc: svc.apply_diff(model.diff, dry_run=model.dryRun),
        transactional=True,
    )


async def _multi_file_patch(args: dict[str, Any], ctx: ToolContext) -> tuple[str, Any]:
    model = MultiFilePatchInput(**_present(args))
    return await _dispatch(
        ctx, "multi_file_patch", kind="write", path="", verb="Applied patches",
        run=lambda svc: svc.multi_file_patch(model.patches, atomic=model.atomic, dry_run=model.dryRun),
        transactional=True,
    )


async def _file_metadata(args: dict[str, Any], ctx: ToolContext) -> tuple[str, Any]:
    model = FileMetadataInput(**_present(args))
    return await _dispatch(
        ctx, "file_metadata", kind="read", path=model.path, verb="Inspected",
        run=lambda svc: svc.file_metadata(model.path),
    )


async def _file_hash(args: dict[str, Any], ctx: ToolContext) -> tuple[str, Any]:
    model = FileHashInput(**_present(args))
    return await _dispatch(
        ctx, "file_hash", kind="read", path=model.path, verb="Hashed",
        run=lambda svc: svc.file_hash(model.path),
    )


async def _file_permissions(args: dict[str, Any], ctx: ToolContext) -> tuple[str, Any]:
    model = FilePermissionsInput(**_present(args))
    return await _dispatch(
        ctx, "file_permissions", kind="read", path=model.path, verb="Checked permissions for",
        run=lambda svc: svc.file_permissions(model.path),
    )


async def _index_workspace(args: dict[str, Any], ctx: ToolContext) -> tuple[str, Any]:
    model = IndexWorkspaceInput(**_present(args))
    text, payload = await _dispatch(
        ctx, "index_workspace", kind="read", path=".", verb="Indexed",
        run=lambda svc: svc.index_workspace(force=model.force),
    )
    if isinstance(payload, dict) and payload.get("status") == "ok":
        await _emit(ctx, "fs.index.updated", {
            "fileCount": payload.get("fileCount", 0),
            "updated": (payload.get("update") or {}).get("updated", 0),
            "removed": (payload.get("update") or {}).get("removed", 0),
            "force": model.force,
        })
        text = (
            f"Indexed {payload.get('fileCount', 0)} file(s) "
            f"({payload.get('totalBytes', 0)} bytes)."
        )
    return text, payload


async def _watch_file(args: dict[str, Any], ctx: ToolContext) -> tuple[str, Any]:
    model = WatchPathInput(**_present(args))
    return await _dispatch(
        ctx, "watch_file", kind="read", path=model.path, verb="Watching",
        run=lambda svc: svc.watch([model.path], recursive=model.recursive),
    )


async def _watch_directory(args: dict[str, Any], ctx: ToolContext) -> tuple[str, Any]:
    model = WatchPathInput(**_present(args))
    return await _dispatch(
        ctx, "watch_directory", kind="read", path=model.path, verb="Watching",
        run=lambda svc: svc.watch([model.path], recursive=model.recursive),
    )


async def _edit_file(args: dict[str, Any], ctx: ToolContext) -> tuple[str, Any]:
    """High-level orchestration: parse the instruction, inspect, patch minimally, verify."""
    model = EditFileInput(**_present(args))
    started = time.monotonic()
    try:
        service = _write_service(ctx)
        packet = service.inspect(model.path, query=model.query)
        old_text, new_text, operation = model.oldText, model.newText, "replace"
        derived: Optional[dict[str, Any]] = None
        if old_text is None or new_text is None:
            # No explicit oldText/newText: derive a concrete minimal edit from the
            # instruction deterministically. Never guess — a vague instruction
            # yields None and falls through to the inspection packet.
            parsed = parse_edit_instruction(model.instruction)
            if parsed is not None:
                derived = parsed.to_dict()
                operation = parsed.operation
                old_text = parsed.old_text
                new_text = parsed.new_text
        if old_text is None or (new_text is None and operation != "delete"):
            # Still nothing concrete: return the inspection packet so the model can
            # choose an exact anchor. This is a successful plan, not a failure.
            await _emit(ctx, "fs.edit_file.inspect", {
                "path": packet.get("path"), "instruction": model.instruction[:200],
                "regions": packet.get("regionCount", 0),
            })
            header = (
                f"Inspection of {packet.get('path')} ({packet.get('language')}, "
                f"{packet.get('totalLines')} lines, sha256={packet.get('sha256')})."
            )
            lines = [header, f"Instruction: {model.instruction}"]
            for symbol in packet.get("symbols", [])[:40]:
                lines.append(f"  {symbol['kind']} {symbol['name']} @ line {symbol['line']}")
            for region in packet.get("regions", [])[:10]:
                lines.append(f"--- lines {region['startLine']}-{region['endLine']} ---")
                lines.extend(region["context"])
            lines.append(
                "Call edit_file again with oldText/newText (and expectedHash "
                f"{packet.get('sha256')}) to apply the minimal change."
            )
            return "\n".join(lines), packet
        if operation == "delete":
            result = service.delete_text(
                model.path, old_text=old_text,
                expected_hash=model.expectedHash or packet.get("sha256"),
                dry_run=model.dryRun,
            )
        elif operation in ("append", "prepend"):
            result = service.insert_text(
                model.path, new_text or "", position=operation,
                expected_hash=model.expectedHash or packet.get("sha256"),
                dry_run=model.dryRun,
            )
        elif operation == "insert":
            position = "before" if is_insert_before(model.instruction) else "after"
            result = service.insert_text(
                model.path, new_text or "", anchor=old_text, position=position,
                expected_hash=model.expectedHash or packet.get("sha256"),
                dry_run=model.dryRun,
            )
        else:
            result = service.patch_file(
                model.path, "replace", old_text=old_text, new_text=new_text,
                expected_hash=model.expectedHash or packet.get("sha256"),
                dry_run=model.dryRun,
            )
    except FsError as exc:
        await _emit(ctx, "fs.edit_file.refused", {"code": exc.code, "path": model.path})
        return _refusal_text(exc), exc.to_dict()
    if derived is not None:
        result["parsedInstruction"] = derived
    previews = _event_previews(result)
    await _emit(ctx, "fs.edit_file.applied", {
        "path": result.get("path"), "bytesWritten": result.get("bytesWritten", 0),
        "parsed": derived is not None,
        "durationMs": int((time.monotonic() - started) * 1000),
        **({"previews": previews} if previews else {}),
    })
    return _summary(result, verb="Edited", path=result.get("path", model.path)), result


async def _verify_file(args: dict[str, Any], ctx: ToolContext) -> tuple[str, Any]:
    model = VerifyFileInput(**_present(args))
    text, payload = await _dispatch(
        ctx, "verify_file", kind="read", path=model.path, verb="Verified",
        run=lambda svc: svc.verify_file(model.path, expected_hash=model.expectedHash),
    )
    if isinstance(payload, dict) and payload.get("status") == "ok":
        syntax = payload.get("syntax") or {}
        if payload.get("binary"):
            text = f"{payload['path']} is binary; nothing to validate."
        else:
            verdict = "OK" if payload.get("verified") else "FAILED"
            detail = ""
            if syntax and not syntax.get("valid"):
                detail = f" — {syntax.get('error')}" + (f" (line {syntax['line']})" if syntax.get("line") else "")
            text = f"{payload['path']}: syntax {verdict} (checker: {syntax.get('checker', 'none')}){detail}"
    return text, payload


async def _validate_changes(args: dict[str, Any], ctx: ToolContext) -> tuple[str, Any]:
    model = ValidateChangesInput(**_present(args))
    text, payload = await _dispatch(
        ctx, "validate_changes", kind="read", path="", verb="Validated changes",
        run=lambda svc: svc.validate_changes(model.edits),
    )
    if isinstance(payload, dict) and payload.get("status") == "ok":
        lines = [f"Changes {'valid' if payload['valid'] else 'INVALID'} ({len(payload['checks'])} checked):"]
        for check in payload["checks"]:
            mark = "ok" if check["ok"] else f"FAIL {check.get('code', '')}"
            lines.append(f"  [{mark}] {check['path']}" + (f" — {check.get('message', '')}" if not check["ok"] else ""))
        text = "\n".join(lines)
    return text, payload


async def _git_status(args: dict[str, Any], ctx: ToolContext) -> tuple[str, Any]:
    text, payload = await _dispatch(
        ctx, "git_status", kind="read", path=".", verb="Checked git status",
        run=lambda svc: svc.git_status(),
    )
    if isinstance(payload, dict) and payload.get("status") == "ok":
        if not payload.get("available"):
            text = f"Not a git checkout: {payload.get('reason', 'git is unavailable')}."
        else:
            counts = payload.get("counts") or {}
            summary = ", ".join(f"{n} {state}" for state, n in sorted(counts.items())) or "clean"
            lines = [f"Git: {summary} (branch {payload.get('branch')}, {payload.get('head', '')[:8]})"]
            for entry in payload.get("entries", [])[:100]:
                lines.append(f"  {entry['state']} {entry['path']}")
            text = "\n".join(lines)
    return text, payload


# ── registration ────────────────────────────────────────────────────────────

_READ_TOOLS = (
    "list_directory", "read_file", "search_text", "search_files", "find_files",
    "search_symbols", "find_references", "file_metadata", "file_hash",
    "file_permissions", "index_workspace", "watch_file", "watch_directory",
    "verify_file", "validate_changes",
)
_WRITE_TOOLS = (
    "write_file", "create_file", "delete_file", "move_file", "copy_file",
    "rename_file", "create_directory", "delete_directory", "patch_file",
    "replace_text", "insert_text", "delete_text", "apply_diff",
    "multi_file_patch", "edit_file",
)

_HANDLERS: dict[str, Any] = {
    "list_directory": _list_directory,
    "read_file": _read_file,
    "write_file": _write_file,
    "create_file": _create_file,
    "delete_file": _delete_file,
    "move_file": _move_file,
    "copy_file": _copy_file,
    "rename_file": _rename_file,
    "create_directory": _create_directory,
    "delete_directory": _delete_directory,
    "search_text": _search_text,
    "search_files": _search_files,
    "find_files": _find_files,
    "search_symbols": _search_symbols,
    "find_references": _find_references,
    "patch_file": _patch_file,
    "replace_text": _replace_text,
    "insert_text": _insert_text,
    "delete_text": _delete_text,
    "apply_diff": _apply_diff,
    "multi_file_patch": _multi_file_patch,
    "file_metadata": _file_metadata,
    "file_hash": _file_hash,
    "file_permissions": _file_permissions,
    "index_workspace": _index_workspace,
    "watch_file": _watch_file,
    "watch_directory": _watch_directory,
    "edit_file": _edit_file,
    "verify_file": _verify_file,
    "validate_changes": _validate_changes,
    "git_status": _git_status,
}

_DESCRIPTIONS: dict[str, str] = {
    "list_directory": "List a folder (recursive/depth/limit/hidden/filter/sort). Read-only; bounded.",
    "read_file": "Read a file with line numbers, by line/offset/limit. Refuses binary. Read-only.",
    "write_file": "Replace a file's contents intentionally. Supports create-if-missing, overwrite policy and a stale-hash guard.",
    "create_file": "Create a new file. Refuses if it already exists unless overwrite is set.",
    "delete_file": "Delete a file (never a folder).",
    "move_file": "Move a file or folder inside the project.",
    "copy_file": "Copy a file or folder inside the project.",
    "rename_file": "Rename a file within its own folder.",
    "create_directory": "Create a folder, recursively by default.",
    "delete_directory": "Remove a folder; a non-empty folder needs recursive=true.",
    "search_text": "Search file contents (literal or regex) via ripgrep with a bounded fallback. Returns path:line:column.",
    "search_files": "Find files by name/path glob.",
    "find_files": "DEPRECATED - use search_files, which is the same implementation and takes these filters too.",
    "search_symbols": "Find functions/classes/types/etc. with AST-aware parsing where available.",
    "find_references": "Find usages of a symbol (index-backed when available, else a word scan).",
    "patch_file": "One minimal edit (replace/insert/delete/append/prepend) with a stale-hash guard.",
    "replace_text": "Replace exact or regex text, with a required/expected occurrence count.",
    "insert_text": "Insert text before/after an anchor, at a line, or at an offset.",
    "delete_text": "Delete exact text or a line range.",
    "apply_diff": "Apply a standard unified diff; the whole diff is validated before any write.",
    "multi_file_patch": "Apply many patches atomically — a failure applies nothing.",
    "file_metadata": "Type, size, mime, encoding, timestamps, permissions, symlink info.",
    "file_hash": "SHA-256 of a file (cached by mtime+size).",
    "file_permissions": "Normalized read/write/execute plus platform-specific detail.",
    "index_workspace": "Build/refresh the separate workspace index (files/symbols/identifiers), incrementally.",
    "watch_file": "Watch a file for created/modified/deleted/renamed events; invalidates caches and re-indexes.",
    "watch_directory": "Watch a folder for changes; invalidates caches and re-indexes incrementally.",
    "edit_file": "High-level edit: inspect the file, locate the region, apply a minimal patch, verify. Prefers minimal changes.",
    "verify_file": "Verify a file after an edit: it exists, is readable, its hash is unchanged, and it parses. Read-only.",
    "validate_changes": "Check a set of proposed edits without applying them: path allowed, target present/missing as declared, hash intact, span unambiguous. Read-only.",
    "git_status": "Report git state (modified/untracked/staged/deleted/renamed + branch/head) when the folder is a checkout. Read-only; never required.",
}


def _definition(name: str) -> ToolDefinition:
    write = name in _WRITE_TOOLS
    # Round 3 / W5 (ADR-H284): `find_files` and `search_files` were always ONE
    # implementation with two front-doors. `search_files` is now the canonical
    # one and carries every filter; `find_files` stays callable for a release
    # and the registry warns once per run naming its replacement.
    replacement = {
        "find_files": "search_files",
        # Round 3 / W3 (ADR-H282): the ONE text-edit tool. `replace_text` and
        # `patch_file` were the same operation as `code_edit`; `edit_file` was a
        # natural-language front-door to it; `multi_file_patch` was the
        # multi-file twin of `apply_diff`. The genuinely DIFFERENT operations
        # stay: `write_file`, `create_file`, `delete_file`, `move_file`,
        # `copy_file`, `rename_file`, the directory tools, `insert_text`,
        # `delete_text` and `apply_diff`.
        "replace_text": "code_edit",
        "patch_file": "code_edit",
        "edit_file": "code_edit",
        "multi_file_patch": "apply_diff",
    }.get(name)
    risk = {
        "delete_file": "medium", "delete_directory": "high", "move_file": "medium",
        "multi_file_patch": "high", "apply_diff": "medium", "write_file": "medium",
    }.get(name, "low" if not write else "medium")
    return ToolDefinition(
        name=name,
        description=_DESCRIPTIONS[name],
        input_schema=_INPUT_SCHEMAS[name],
        handler=_HANDLERS[name],
        permission="write" if write else "read",
        risk=risk,
        timeout_ms=60_000 if write else 30_000,
        effect="write" if write else None,
        deprecated=replacement is not None,
        replacement=replacement,
        parallel_safe=(not write and name in _READ_TOOLS and name not in {"index_workspace", "watch_file", "watch_directory"}),
    )


def register_fs_tools(registry: ToolRegistry) -> list[str]:
    """Register the file-system tools. Returns the names added."""
    names = list(_READ_TOOLS) + list(_WRITE_TOOLS)
    for name in names:
        registry.register(_definition(name))
    return names


__all__ = [
    "register_fs_tools",
    "FS_READ_TOOL_NAMES",
    "FS_WRITE_TOOL_NAMES",
]

FS_READ_TOOL_NAMES = _READ_TOOLS
FS_WRITE_TOOL_NAMES = _WRITE_TOOLS
