"""The five read-only repository tools (Phase 1).

`register_code_tools(registry)` exposes the read half of the code capability:
list, search, read with line numbers, find things in the docs, and report the
checkout status. Every tool declares `permission="read"`, so the registry's
non-read gate never fires: a read tool is callable by an allowlisted run and
needs no approval. There is NO write tool, NO patch tool and NO command runner —
the only child process is the fixed, argument-free `git` probe in `workspace.py`.

Every handler returns `(text, data)`: `text` is the bounded, model-facing prose
(bounded again at the registry seam, ADR-H91); `data` is the structured record
the front door reads. Nothing here fabricates: a missing workspace, a missing
file or an absent git all produce an explicit refusal, never an invented value.
"""

from __future__ import annotations

import os
import re
import time
from pathlib import Path
from typing import Any, Iterator, Optional, Tuple

from pydantic import BaseModel, ConfigDict, Field

from ..tools import ToolContext, ToolDefinition, ToolRegistry
from . import commands as commands_mod
from .scan import iter_scan_files as _iter_scan_files  # noqa: PLC0415
from . import policy
from . import patch as patch_mod
from . import sandbox as sandbox_mod
from . import worktree as worktree_mod
from .commands import OPERATOR_GATED_CLASSES, CommandUnavailable, build_argv, resolve_class
from .patch import MAX_OPERATIONS, MAX_REASON_CHARS, PatchError, parse_patch
from .sandbox import SandboxUnavailable
from .worktree import CodeWorkspace, WorkspaceError
from .workspace import (
    MAX_LIST_ENTRIES,
    MAX_MATCHES,
    MAX_PATTERN_CHARS,
    MAX_READ_LINES,
    MAX_SCAN_BYTES,
    MAX_SCAN_FILES,
    ResolvedWorkspace,
    WorkspaceDenial,
    confine,
    git_probe,
    is_binary,
    walk,
)
from ..session_index import redact_span, secret_pattern_name

_DOCS_WINDOW_LINES = 6


# ── Input models (pydantic, extra="forbid") ─────────────────────────────────


class WorkspaceStatusInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class WorkspaceListInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = "."
    depth: int = Field(2, ge=1, le=4)
    includeSizes: bool = False  # noqa: N815
    maxEntries: int = Field(200, ge=1, le=MAX_LIST_ENTRIES)  # noqa: N815


class WorkspaceSearchInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pattern: str = Field(..., min_length=1, max_length=MAX_PATTERN_CHARS)
    path: str = "."
    maxMatches: int = Field(50, ge=1, le=MAX_MATCHES)  # noqa: N815
    contextLines: int = Field(2, ge=0, le=20)  # noqa: N815
    caseSensitive: bool = False  # noqa: N815


class WorkspaceReadInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(..., min_length=1, max_length=1024)
    startLine: int = Field(1, ge=1)  # noqa: N815
    maxLines: int = Field(400, ge=1, le=MAX_READ_LINES)  # noqa: N815


class WorkspaceDocsInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(..., min_length=1, max_length=200)
    path: str = "docs"
    maxResults: int = Field(5, ge=1, le=20)  # noqa: N815
    maxChars: int = Field(4_000, ge=200, le=8_000)  # noqa: N815


class _PatchOpModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    op: str = Field(..., min_length=1, max_length=16)
    path: str = Field(..., min_length=1, max_length=512)
    reason: str = Field("", max_length=MAX_REASON_CHARS)
    content: Optional[str] = None
    old: Optional[str] = None
    new: Optional[str] = None
    expectHash: Optional[str] = Field(default=None, max_length=128)  # noqa: N815
    fromPath: Optional[str] = Field(default=None, max_length=512)  # noqa: N815


class PatchApplyInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operations: list[_PatchOpModel] = Field(..., min_length=1, max_length=MAX_OPERATIONS)
    scope: list[str] = Field(default_factory=list, max_length=16)
    note: str = Field("", max_length=MAX_REASON_CHARS)


class WorkspaceDiffInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base: Optional[str] = Field(default=None, max_length=128)  # noqa: N815
    includeUnified: bool = True  # noqa: N815


class WorkspaceRevertInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    applied: list[dict] = Field(..., min_length=1)


class CommandRunInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # A plain string, NOT a Literal: an unlisted class must reach the handler so
    # the refusal is recorded (a schema rejection leaves no row and no event).
    command_class: str = Field(..., min_length=1, max_length=64)
    args: dict[str, Any] = Field(default_factory=dict)
    note: str = Field("", max_length=200)


from ..delegation_tools import delegate_schema as _delegate_schema  # noqa: E402


_INPUT_SCHEMAS: dict[str, dict[str, Any]] = {
    "workspace_status": {"type": "object", "properties": {}, "additionalProperties": False},
    "workspace_list": {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Folder to list, relative to the project root (default '.')."},
            "depth": {"type": "integer", "description": "How deep to descend (1-4, default 2)."},
            "includeSizes": {"type": "boolean", "description": "Include each file's size in bytes."},
            "maxEntries": {"type": "integer", "description": "Maximum entries to return (default 200)."},
        },
        "additionalProperties": False,
    },
    "workspace_search": {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "Regular expression to find in file contents."},
            "path": {"type": "string", "description": "Folder, or a single file, to search, relative to the project root (default '.')."},
            "maxMatches": {"type": "integer", "description": "Maximum matches to return (default 50)."},
            "contextLines": {"type": "integer", "description": "Lines of context around each match (0-20, default 2)."},
            "caseSensitive": {"type": "boolean", "description": "Match case-sensitively (default false)."},
        },
        "required": ["pattern"],
        "additionalProperties": False,
    },
    "workspace_read": {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "File to read, relative to the project root."},
            "startLine": {"type": "integer", "description": "First line to return (1-based; use the previous nextStartLine to continue)."},
            "maxLines": {"type": "integer", "description": "Maximum lines to return (default 400)."},
        },
        "required": ["path"],
        "additionalProperties": False,
    },
    "workspace_docs": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "What to look for in the project's notes and documentation."},
            "path": {"type": "string", "description": "Folder to search for notes/documentation (default 'docs')."},
            "maxResults": {"type": "integer", "description": "Maximum excerpts to return (default 5)."},
            "maxChars": {"type": "integer", "description": "Total character budget for excerpts (default 4000)."},
        },
        "required": ["query"],
        "additionalProperties": False,
    },
    "workspace_patch_apply": {
        "type": "object",
        "properties": {
            "operations": {
                "type": "array",
                "description": "The changes to make, in order. Each is create/replace/delete/move.",
                "items": {
                    "type": "object",
                    "properties": {
                        "op": {"type": "string", "enum": ["create", "replace", "delete", "move"]},
                        "path": {"type": "string", "description": "Destination path, relative to the project."},
                        "reason": {"type": "string", "description": "Why this change is being made."},
                        "content": {"type": "string", "description": "create: the whole file content."},
                        "old": {"type": "string", "description": "replace: the exact text to replace."},
                        "new": {"type": "string", "description": "replace: the replacement text."},
                        "expectHash": {"type": "string", "description": "Optional hash the file is expected to have now."},
                        "fromPath": {"type": "string", "description": "move: the source path."},
                    },
                    "required": ["op", "path"],
                },
            },
            "scope": {"type": "array", "items": {"type": "string"}, "description": "Optional declared path prefixes the change is limited to."},
            "note": {"type": "string", "description": "A short note about the change."},
        },
        "required": ["operations"],
        "additionalProperties": False,
    },
    "workspace_diff": {
        "type": "object",
        "properties": {
            "base": {"type": "string", "description": "Optional revision to compare against (defaults to the task's start)."},
            "includeUnified": {"type": "boolean", "description": "Include the raw unified change text (default true)."},
        },
        "additionalProperties": False,
    },
    "workspace_revert": {
        "type": "object",
        "properties": {
            "applied": {"type": "array", "items": {"type": "object"}, "description": "The change record returned by a prior apply."},
        },
        "required": ["applied"],
        "additionalProperties": False,
    },
    "command_run": {
        "type": "object",
        "properties": {
            "command_class": {
                # A plain string (not an enum): an unlisted class must reach the
                # handler so the refusal is RECORDED, not silently rejected at the
                # schema seam with no row and no event.
                "type": "string",
                "description": "Which check to run: typecheck, lint, unit_test, python_test, build, format_check, git_read, or dependency_install.",
            },
            "args": {"type": "object", "description": "Typed arguments for the class (e.g. {subcommand} for git_read)."},
            "note": {"type": "string", "description": "Why this check is wanted; recorded in the trace."},
        },
        "required": ["command_class"],
        "additionalProperties": False,
    },
    "code_action": {
        "type": "object",
        "properties": {
            "message": {"type": "string", "description": "A short description of the change (commit message / review-request title)."},
            "baseRef": {"type": "string", "description": "For a review request: the branch to compare against."},
            "headRef": {"type": "string", "description": "The branch the change is on."},
            "argv": {"type": "array", "items": {"type": "string"}, "description": "Reserved; the action has a fixed implementation."},
        },
        "additionalProperties": False,
    },
    "delegate": _delegate_schema(),
}
from ..analysis_tools import analysis_schema as _analysis_schema  # noqa: E402
from ..scaffold_tools import scaffold_schema as _scaffold_schema  # noqa: E402

_INPUT_SCHEMAS["analysis"] = _analysis_schema()
_INPUT_SCHEMAS["scaffold"] = _scaffold_schema()


# ── Events ──────────────────────────────────────────────────────────────────

# The single `code.*` event seam. Extracted to `code/events.py`; re-exported here
# so every existing `from .tools import emit_code_event` keeps working.
# `REDACT_MARKER` + `emit_patch_applied` live there too, as the one copy.
from .events import REDACT_MARKER, emit_code_event, emit_patch_applied  # noqa: E402,F401


def _present(args: dict[str, Any]) -> dict[str, Any]:
    """Drop keys whose value is None.

    The registry builds its generic arg model with `Optional[...] = None` for
    every non-required field, so an omitted optional arrives here as an explicit
    None. Our stricter pydantic models need the field to be ABSENT so their own
    defaults apply.
    """
    return {k: v for k, v in args.items() if v is not None}


def _refusal(tool: str, denial: WorkspaceDenial) -> tuple[str, Any]:
    return (
        f"REFUSED ({denial.code}): {denial.reason}",
        {"status": "refused", "code": denial.code, "path": denial.path, "tool": tool},
    )


def _no_workspace(tool: str) -> tuple[str, Any]:
    return (
        "REFUSED (WORKSPACE_UNAVAILABLE): There is no project folder available to look through.",
        {"status": "refused", "code": "WORKSPACE_UNAVAILABLE", "tool": tool},
    )


# ── workspace_status ────────────────────────────────────────────────────────


async def _status(args: dict[str, Any], ctx: ToolContext) -> tuple[str, Any]:
    ws: Optional[ResolvedWorkspace] = getattr(ctx, "workspace", None)
    if ws is None:
        await emit_code_event(ctx, "code.refused", {"tool": "workspace_status", "code": "WORKSPACE_UNAVAILABLE"})
        return _no_workspace("workspace_status")
    head = git_probe(ws, "head")
    branch = git_probe(ws, "branch")
    status = git_probe(ws, "status")
    await emit_code_event(
        ctx, "code.workspace.bound",
        {"workspaceId": ws.id, "strategy": ws.strategy, "baseRevision": ws.base_revision},
    )
    if not status.get("available"):
        text = (
            "I can't tell the version of this project: "
            f"{status.get('reason', 'git is unavailable')}. Nothing was changed by this task."
        )
        return text, {"status": "unavailable", "workspaceId": ws.id, "strategy": ws.strategy}
    dirty = status.get("dirty") or []
    direct = bool(getattr(ctx, "direct", False))
    lines = [
        f"Project root: {ws.root}",
        f"Where changes land: {'the user\'s real folder (direct)' if direct else 'a disposable copy of the project (the user reviews before accepting)'}",
        f"Revision: {head.get('head', 'unknown') if head.get('available') else 'unknown'}",
        f"Current line of work: {branch.get('branch', 'unknown') if branch.get('available') else 'unknown'}",
    ]
    if dirty:
        lines.append(f"{len(dirty)} file(s) already differ from the last saved version (none changed by this task):")
        for entry in dirty[:50]:
            lines.append(f"  {entry['status']} {entry['path']} (pre-existing)")
    else:
        lines.append("No files differ from the last saved version.")
    text = "\n".join(lines)
    data = {
        "status": "ok",
        "workspaceId": ws.id,
        "strategy": ws.strategy,
        "direct": direct,
        "baseRevision": ws.base_revision,
        "head": head.get("head") if head.get("available") else None,
        "branch": branch.get("branch") if branch.get("available") else None,
        "dirty": dirty,
    }
    return text, data


# ── workspace_list ──────────────────────────────────────────────────────────


async def _list(args: dict[str, Any], ctx: ToolContext) -> tuple[str, Any]:
    ws: Optional[ResolvedWorkspace] = getattr(ctx, "workspace", None)
    if ws is None:
        await emit_code_event(ctx, "code.refused", {"tool": "workspace_list", "code": "WORKSPACE_UNAVAILABLE"})
        return _no_workspace("workspace_list")
    model = WorkspaceListInput(**_present(args))
    try:
        entries, truncated = walk(
            ws, model.path, depth=model.depth, max_entries=model.maxEntries,
        )
    except WorkspaceDenial as denial:
        await emit_code_event(ctx, "code.refused", {"tool": "workspace_list", "code": denial.code, "path": denial.path})
        return _refusal("workspace_list", denial)
    lines = [f"Contents of {model.path} (depth {model.depth}):"]
    for entry in entries:
        suffix = ""
        if model.includeSizes and entry.get("type") == "file" and "size" in entry:
            suffix = f"  ({entry['size']} bytes)"
        lines.append(f"  {entry['path']}{suffix}")
    if truncated:
        lines.append(f"[showing the first {len(entries)} entries; the list continues]")
    await emit_code_event(ctx, "code.listed", {"path": model.path, "entries": len(entries), "truncated": truncated})
    return "\n".join(lines), {
        "status": "ok",
        "path": model.path,
        "entries": entries,
        "count": len(entries),
        "truncated": truncated,
    }


# ── workspace_search ────────────────────────────────────────────────────────


async def _search(args: dict[str, Any], ctx: ToolContext) -> tuple[str, Any]:
    """Search file contents - a DEPRECATED ALIAS of the fs subsystem's search.

    Round 3 / W5 (ADR-H284): this used to run its OWN naive Python scan while
    the fs subsystem's `search_text` was ALREADY ripgrep-first. The tool the
    model reached for FIRST was therefore the slow one - and the fast one was
    one name away in a surface that listed both.

    The two are now ONE implementation. This delegates to it, so the same
    engine answers, and the engine ALWAYS says which one it was - a slow result
    is explainable rather than mysterious.

    It still works: deprecation is advisory, and the registry warns once per
    run naming `search_text`. The legacy payload field `filesScanned` is gone
    (nothing read it) and is replaced by `engine` + `filesWithMatches`, both of
    which are exact.
    """
    ws: Optional[ResolvedWorkspace] = getattr(ctx, "workspace", None)
    if ws is None:
        await emit_code_event(ctx, "code.refused", {"tool": "workspace_search", "code": "WORKSPACE_UNAVAILABLE"})
        return _no_workspace("workspace_search")
    model = WorkspaceSearchInput(**_present(args))
    from .fs import errors as fs_err  # noqa: PLC0415
    from .fs_tools import _read_service  # noqa: PLC0415

    try:
        result = _read_service(ctx).search_text(
            model.pattern,
            path=model.path,
            regex=True,
            case_sensitive=model.caseSensitive,
            max_results=model.maxMatches,
            context_lines=model.contextLines,
        )
    except fs_err.FsError as exc:
        await emit_code_event(ctx, "code.refused", {
            "tool": "workspace_search", "code": exc.code, "path": exc.path,
        })
        return f"REFUSED ({exc.code}): {exc.reason}", exc.to_dict()

    matches = list(result.get("matches") or [])
    engine = str(result.get("engine") or "unknown")
    lines = [f"{len(matches)} match(es) for /{model.pattern}/ (via {engine}):"]
    for match in matches:
        text = str(match.get("text") or "").strip()
        lines.append(f"{match.get('path')}:{match.get('line')}: {text}")
    if result.get("truncated"):
        lines.append(f"[results truncated after {len(matches)} matches]")
    await emit_code_event(ctx, "code.searched", {
        "pattern": model.pattern, "matches": len(matches), "engine": engine,
        "truncated": bool(result.get("truncated")), "ms": result.get("ms"),
    })
    return "\n".join(lines), {
        "status": "ok",
        "pattern": model.pattern,
        "engine": engine,
        "matches": matches,
        "count": len(matches),
        "filesWithMatches": len({str(m.get("path")) for m in matches}),
        "truncated": bool(result.get("truncated")),
        "ms": result.get("ms"),
    }

# ── workspace_read ──────────────────────────────────────────────────────────


def _file_hash(target: Path) -> str:
    """The file's sha256 - the token the read ledger and the edit guard share."""
    from .fs.metadata import hash_file  # noqa: PLC0415

    try:
        return hash_file(target)
    except OSError:
        return ""


async def _read(args: dict[str, Any], ctx: ToolContext) -> tuple[str, Any]:
    ws: Optional[ResolvedWorkspace] = getattr(ctx, "workspace", None)
    if ws is None:
        await emit_code_event(ctx, "code.refused", {"tool": "workspace_read", "code": "WORKSPACE_UNAVAILABLE"})
        return _no_workspace("workspace_read")
    model = WorkspaceReadInput(**_present(args))
    try:
        target = confine(ws, model.path)
    except WorkspaceDenial as denial:
        # Protected/ignored/escape all fire BEFORE a byte is read, so `jdds.db`
        # reports PROTECTED_PATH and never BINARY_FILE.
        await emit_code_event(ctx, "code.refused", {"tool": "workspace_read", "code": denial.code, "path": denial.path})
        return _refusal("workspace_read", denial)
    if is_binary(target):
        await emit_code_event(ctx, "code.refused", {"tool": "workspace_read", "code": "BINARY_FILE", "path": model.path})
        return (
            "REFUSED (BINARY_FILE): That file isn't readable text, so Eclipse left it alone.",
            {"status": "refused", "code": "BINARY_FILE", "path": model.path},
        )
    try:
        raw_lines = target.read_text(encoding="utf-8", errors="strict").splitlines()
    except (UnicodeDecodeError, OSError):
        await emit_code_event(ctx, "code.refused", {"tool": "workspace_read", "code": "BINARY_FILE", "path": model.path})
        return (
            "REFUSED (BINARY_FILE): That file isn't readable text, so Eclipse left it alone.",
            {"status": "refused", "code": "BINARY_FILE", "path": model.path},
        )
    start = model.startLine - 1
    window = raw_lines[start:start + model.maxLines]
    redacted_lines: list[int] = []
    pattern_names: set[str] = set()
    numbered: list[str] = []
    for offset, line in enumerate(window):
        line_no = start + offset + 1
        cleaned = redact_span(line, REDACT_MARKER)
        if cleaned != line.strip() and REDACT_MARKER in cleaned:
            redacted_lines.append(line_no)
            name = secret_pattern_name(line)
            if name:
                pattern_names.add(name)
        numbered.append(f"{line_no:>6}\t{cleaned}")
    continues = start + len(window) < len(raw_lines)
    next_start = (start + len(window) + 1) if continues else None
    rel = target.relative_to(ws.root).as_posix()
    header = f"{rel} (lines {start + 1}-{start + len(window)} of {len(raw_lines)})"
    body = [header, *numbered]
    if continues:
        body.append(f"[continues — call workspace_read again with startLine={next_start}]")
    if redacted_lines:
        body.append(f"[{len(redacted_lines)} line(s) redacted for privacy]")
    await emit_code_event(ctx, "code.read", {
        "path": rel, "startLine": model.startLine, "lines": len(window),
        "nextStartLine": next_start, "redactedLines": redacted_lines,
    })
    if redacted_lines:
        await emit_code_event(ctx, "code.redacted", {
            "path": rel, "lines": redacted_lines,
            "patternName": sorted(pattern_names)[0] if pattern_names else "secret",
            "patternNames": sorted(pattern_names),
        })
    return "\n".join(body), {
        "status": "ok",
        "path": rel,
        "startLine": model.startLine,
        "lines": len(window),
        "totalLines": len(raw_lines),
        "nextStartLine": next_start,
        "redactedLines": redacted_lines,
        "content": numbered,
        # Round 3 / W3 (ADR-H282): the hash of the WHOLE file, so this read
        # feeds the per-run read ledger and the one edit tool can insist the
        # file was read before it is changed.
        "sha256": _file_hash(target),
    }


# ── Phase 2: the write half (patch / diff / revert) ─────────────────────────
#
# A code write is authorised by the run's autonomy profile, not by an operator
# approval (Phase 5 owns that). The disposable CodeWorkspace is allocated lazily
# on the first write, from the SAME server-resolved root the Phase-1 navigator
# uses — never the operator's checkout. The binding/actor are rebuilt against the
# worktree root so `policy.decide_write` confines the write to the disposable tree.


def _present_dict(raw: Any) -> dict[str, Any]:
    return raw if isinstance(raw, dict) else {}


def _autonomy_grants_write(ctx: ToolContext) -> bool:
    autonomy = getattr(ctx, "autonomy", None)
    return bool(autonomy and "code.write" in policy.code_grants(str(autonomy)))


def _ensure_code_workspace(ctx: ToolContext) -> tuple[CodeWorkspace, Any, ResolvedWorkspace, Any]:
    """Allocate (once) the disposable workspace for this run.

    Returns `(code_workspace, binding, resolved_workspace, actor)`. The binding's
    root is the worktree root, so `policy.decide_write` and `workspace.confine`
    both act on the disposable copy.
    """
    existing = getattr(ctx, "_code_workspace", None)
    if existing is not None:
        return existing
    base = getattr(getattr(ctx, "workspace", None), "base_revision", "HEAD") or "HEAD"
    # A Phase-1 base of "unknown" is not a git revision; fall back to HEAD so the
    # workspace still allocates. Never invent a sha — HEAD is the honest default.
    if not re.fullmatch(worktree_mod.BASE_REVISION_PATTERN, str(base)):
        base = "HEAD"
    cw = worktree_mod.allocate(
        base_revision=base,
        direct=bool(getattr(ctx, "direct", False)),
        root=getattr(getattr(ctx, "workspace", None), "root", None),
    )
    binding = policy.WorkspaceBinding(
        id=cw.id, root=cw.root, base_revision=cw.base_revision, strategy=cw.strategy,
    )
    resolved = ResolvedWorkspace(
        id=cw.id, root=cw.root, base_revision=cw.base_revision,
        strategy=cw.strategy, binding=binding,
    )
    actor = _make_actor(cw.id, getattr(ctx, "autonomy", None))
    bundle = (cw, binding, resolved, actor)
    try:
        ctx._code_workspace = bundle
    except Exception:  # noqa: BLE001 - a frozen ctx must not break the call
        pass
    store = getattr(ctx, "code_store", None)
    if store is not None:
        try:
            store.save_workspace(
                cw.id, str(cw.root), cw.base_revision, cw.strategy,
                run_id=getattr(ctx, "run_id", None), file_count=cw.file_count,
                checkout_root=str(cw.checkout_root) if cw.checkout_root else None,
            )
        except Exception:  # noqa: BLE001 - a record failure must not block a write
            pass
    return bundle


def _make_actor(workspace_id: str, autonomy: Any) -> Any:
    from ..skills.permissions import Actor  # noqa: PLC0415

    return Actor(id="coder", operator=False, workspace_id=workspace_id, autonomy=autonomy)


def make_code_authorizer(mcp_authorizer: Any = None):
    """Compose the effect authorizer for a run's non-read tools.

    A code write tool (`workspace_*`, effect `write`) is admitted at this seam;
    the HANDLER then enforces the binding and the autonomy profile and reports the
    precise refusal code (`WORKSPACE_UNBOUND` / `PROFILE_DENIED`), and the engine
    (`patch.apply` → `policy.decide_write`) re-checks `code.write` for every path.
    The write is therefore gated at three layers. Every other non-read tool falls
    through to the MCP approvals authorizer (single-use operator approval) — or is
    refused when there is none.
    """

    async def authorize(tool: Any, args: dict[str, Any], ctx: Any = None) -> bool:
        name = str(getattr(tool, "name", ""))
        if name.startswith("workspace_") and getattr(tool, "effect", None) == "write":
            # Per-call approval, opencode-style: a patch apply needs a single-use
            # approval keyed to the exact changed paths, OR a workspace-wide
            # "allow session" approval. Without an approvals backend (tests,
            # zero-config local runs) the write stays admitted and the handler
            # still enforces binding + autonomy profile.
            if name == "workspace_patch_apply":
                approvals = getattr(ctx, "approvals", None) if ctx is not None else None
                workspace = getattr(ctx, "workspace", None) if ctx is not None else None
                if approvals is not None and workspace is not None:
                    import hashlib as _hashlib  # noqa: PLC0415

                    ws_id = str(getattr(workspace, "id", ""))
                    try:
                        ops = (args or {}).get("operations") or []
                        paths = sorted(str(op.get("path", "")) for op in ops if isinstance(op, dict))
                    except Exception:  # noqa: BLE001
                        paths = []
                    digest = _hashlib.sha256(("\x00".join(paths)).encode("utf-8")).hexdigest()
                    if approvals.consume_kind(kind="code_write", request_key=ws_id, digest=digest, workspace_id=ws_id) is not None:
                        return True
                    if approvals.find_active_kind(kind="code_write_session", request_key=ws_id, digest=ws_id) is not None:
                        return True
                    pending = None
                    try:
                        pending = approvals.request_kind(
                            kind="code_write", request_key=ws_id, digest=digest,
                            effect="write",
                            run_id=str(getattr(ctx, "run_id", "")),
                            workspace_id=ws_id,
                            action_summary=f"Apply code changes to: {', '.join(paths)[:180]}",
                            risk="medium",
                        )
                    except Exception:  # noqa: BLE001
                        pass
                    try:
                        await emit_code_event(ctx, "approval.requested", {
                            "approvalId": (pending or {}).get("id"),
                            "kind": "code_write",
                            "title": "Approve writing code",
                            "summary": f"Apply code changes to: {', '.join(paths)[:180]}",
                            "effect": "write",
                            "risk": "medium",
                            "paths": paths,
                            "workspaceId": ws_id,
                        })
                    except Exception:  # noqa: BLE001
                        pass
                    # Pause mode: block for the operator's decision, then re-claim.
                    if pending is not None:
                        from ..mcp.approvals import pause_for_decision  # noqa: PLC0415

                        if await pause_for_decision(
                            approvals, ctx, pending.get("id"),
                            lambda: approvals.consume_kind(
                                kind="code_write", request_key=ws_id, digest=digest, workspace_id=ws_id,
                            ),
                            emitter=getattr(ctx, "emitter", None), digest=digest,
                            event_type="code.write.approved",
                            approved_payload={"kind": "code_write", "paths": paths},
                        ):
                            return True
                    return False
            return True
        # A command execution is admitted at this seam; the handler enforces the
        # binding, the autonomy profile (`command.execute`) and the class table,
        # and every path is refused-and-recorded on its own merits.
        if name == "command_run":
            return True
        # A scaffold is admitted at this seam; the handler enforces the binding,
        # the autonomy profile (`code.write`) and the closed `produces` set, and
        # every write is re-checked per path by `patch.apply` → `policy.decide_write`.
        if name == "scaffold_kit" and getattr(tool, "effect", None) == "write":
            return True
        # The fs subsystem's mutating tools AND the CIUE semantic-rename tool are
        # code.write: the handler enforces the binding + autonomy profile, and every
        # path is re-checked by the engine (`policy.decide_write` / `resolve_within`).
        if policy.is_code_write(name) and getattr(tool, "effect", None) == "write":
            return True
        if mcp_authorizer is not None:
            return bool(await mcp_authorizer(tool, args, ctx))
        return False

    return authorize


async def _emit_patch_refused(ctx: ToolContext, workspace_id: str, op: str, path: str, code: str) -> None:
    await emit_code_event(ctx, "code.patch.refused", {
        "workspaceId": workspace_id, "op": op, "path": path, "code": code,
    })
    # A policy denial is ALSO emitted on the shared event family so the Phase 4
    # mistake analyzer can see a scope/protected denial deterministically.
    if code in ("PROTECTED_PATH", "OUTSIDE_WORKSPACE", "PATH_OUTSIDE_WORKSPACE",
                "COMMAND_NOT_ALLOWLISTED", "PROFILE_DENIED"):
        await emit_code_event(ctx, "code.policy.denied", {
            "code": code, "decision": "write", "relPath": path,
        })


def _patch_refusal(workspace_id: str, code: str, reason: str, op: str = "", path: str = "") -> tuple[str, Any]:
    return (
        f"REFUSED ({code}): {reason}",
        {"status": "refused", "code": code, "reason": reason, "op": op, "path": path, "workspaceId": workspace_id},
    )


async def _patch_apply(args: dict[str, Any], ctx: ToolContext) -> tuple[str, Any]:
    model = PatchApplyInput(**_present(args))
    ws_binding = getattr(ctx, "workspace", None)
    if ws_binding is None:
        await _emit_patch_refused(ctx, "", "", "", "WORKSPACE_UNBOUND")
        return _patch_refusal("", "WORKSPACE_UNBOUND", "there is no project folder to make the change in")
    if not _autonomy_grants_write(ctx):
        await _emit_patch_refused(ctx, "", "", "", "PROFILE_DENIED")
        return _patch_refusal("", "PROFILE_DENIED", "this task isn't set up to make that kind of change")
    try:
        ops = parse_patch({"operations": [op.model_dump() for op in model.operations]})
    except PatchError as exc:
        await _emit_patch_refused(ctx, "", "", exc.reason, exc.code)
        return _patch_refusal("", exc.code, exc.reason)
    if len(ops) > MAX_OPERATIONS:
        return _patch_refusal("", "PATCH_TOO_MANY_OPERATIONS", "too many changes at once")
    try:
        cw, binding, resolved, actor = _ensure_code_workspace(ctx)
    except WorkspaceError as exc:
        await _emit_patch_refused(ctx, "", "", exc.reason, exc.code)
        return _patch_refusal("", exc.code, exc.reason)
    await emit_code_event(ctx, "code.workspace.created", {
        "workspaceId": cw.id, "strategy": cw.strategy,
        "baseRevision": cw.base_revision, "fileCount": cw.file_count,
    })

    scope = tuple(str(s) for s in (model.scope or []))
    try:
        result = patch_mod.apply(
            resolved, ops, scope=scope, binding=binding, actor=actor, emitter=ctx.emitter,
        )
    except PatchError as exc:
        await _emit_patch_refused(ctx, cw.id, ops[0].op if ops else "", ops[0].path if ops else "", exc.code)
        return _patch_refusal(cw.id, exc.code, exc.reason, op=ops[0].op if ops else "")
    if not result.get("ok"):
        refused = result.get("refused") or {}
        await _emit_patch_refused(ctx, cw.id, refused.get("op", ""), "", refused.get("code", ""))
        return _patch_refusal(cw.id, refused.get("code", "PATCH_MALFORMED"), refused.get("reason", ""))

    for item in result["applied"]:
        await emit_patch_applied(ctx, cw.id, item)
    summary = ", ".join(f"{a['op']} {a['path']}" for a in result["applied"])
    where = "the user's real folder" if getattr(ctx, "direct", False) else "the task's working copy"
    text = f"Change made in {where}: {summary}."
    data = {
        "status": "ok",
        "workspaceId": cw.id,
        "strategy": cw.strategy,
        "applied": result["applied"],
        "filesChanged": result["filesChanged"],
        "totalBytes": result["totalBytes"],
    }
    # Auto-verify: when the workspace pins a check command (`.black-eclipse/verify.json`,
    # shape {"command": "..."}), run it right after the edit and fold the result into
    # the tool output — the model sees test/typecheck failures as the observation and
    # can fix them, without waiting to be asked. Only `allow`-classified commands run.
    verify_note = _auto_verify(resolved.root)
    if verify_note is not None:
        text += "\n" + verify_note["text"]
        data["verify"] = verify_note["data"]
    return text, data


def _auto_verify(root: Path) -> Optional[dict[str, Any]]:
    import json as _json  # noqa: PLC0415
    from ..terminal import runtime as _terminal_runtime  # noqa: PLC0415
    from . import terminal as _terminal  # noqa: PLC0415

    config = root / ".black-eclipse" / "verify.json"
    try:
        payload = _json.loads(config.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    command = str(payload.get("command") or "").strip()
    if not command:
        return None
    verdict = _terminal.classify(command)
    if verdict.kind != "allow":
        return {
            "text": f"Auto-verify skipped: `{command}` is classified `{verdict.kind}`.",
            "data": {"status": "skipped", "command": command, "verdict": verdict.kind},
        }
    manager = _terminal_runtime.get_terminal_manager()
    if manager is None:
        return {
            "text": f"Auto-verify `{command}` could not run: the terminal isn't available.",
            "data": {"status": "error", "error": "terminal unavailable"},
        }
    try:
        raw = manager.execute_command(
            command=command, cwd=str(root), runtime="local", shell_mode=False,
            timeout_ms=120_000,
        )
    except Exception as exc:  # noqa: BLE001 - verification must never crash the edit
        return {"text": f"Auto-verify `{command}` could not run: {exc}", "data": {"status": "error", "error": str(exc)}}
    if raw.status == "unavailable":
        reason = raw.error.message if raw.error else "the terminal isn't available"
        return {"text": f"Auto-verify `{command}` could not run: {reason}", "data": {"status": "error", "error": reason}}
    digest = (raw.stdout or "")[-2000:]
    ok = raw.exit_code == 0 and not raw.timed_out and raw.status == "ok"
    return {
        "text": f"Auto-verify `{command}`: {'PASS' if ok else 'FAIL'} (exit={raw.exit_code})" + (f"\n{digest}" if digest else ""),
        "data": {"status": "ok" if ok else "failed", "command": command, "exitCode": raw.exit_code, "timedOut": raw.timed_out, "output": digest},
    }


async def _diff(args: dict[str, Any], ctx: ToolContext) -> tuple[str, Any]:
    model = WorkspaceDiffInput(**_present(args))
    if getattr(ctx, "workspace", None) is None:
        return ("REFUSED (WORKSPACE_UNBOUND): there is no project folder to compare.",
                {"status": "refused", "code": "WORKSPACE_UNBOUND"})
    existing = getattr(ctx, "_code_workspace", None)
    if existing is None:
        # Nothing has been written yet this run — a diff is honestly empty.
        return (
            "No changes have been made in this task yet.",
            {"status": "ok", "strategy": "none", "filesChanged": 0, "unified": "",
             "added": [], "modified": [], "deleted": [], "digest": None, "truncated": False,
             "additions": 0, "deletions": 0, "totalChars": 0},
        )
    cw = existing[0]
    try:
        # Rooted at the cut-from project: else the diff reports the harness repo.
        result = worktree_mod.diff(cw.id, model.base, strategy=cw.strategy,
                                   root=getattr(getattr(ctx, "workspace", None), "root", None))
    except WorkspaceError as exc:
        return _patch_refusal(cw.id, exc.code, exc.reason)
    store = getattr(ctx, "code_store", None)
    if store is not None:
        try:
            store.set_diff_digest(cw.id, result["digest"])
        except Exception:  # noqa: BLE001
            pass
    await emit_code_event(ctx, "code.diff", {
        "workspaceId": cw.id, "filesChanged": result["filesChanged"],
        "additions": result["additions"], "deletions": result["deletions"],
        "digest": result["digest"], "truncated": result["truncated"],
    })
    lines = [
        f"{result['filesChanged']} file(s) changed in "
        f"{'the user\'s real folder' if getattr(ctx, 'direct', False) else 'the task\'s working copy'} "
        f"(+{result['additions']}/-{result['deletions']})."
    ]
    if not model.includeUnified:
        result = {**result, "unified": ""}
    else:
        lines.append(result["unified"])
    data = {
        "status": "ok", "strategy": result["strategy"], "base": result["base"],
        "added": result["added"], "modified": result["modified"], "deleted": result["deleted"],
        "digest": result["digest"], "filesChanged": result["filesChanged"],
        "additions": result["additions"], "deletions": result["deletions"],
        "unified": result["unified"], "truncated": result["truncated"],
        "totalChars": result["totalChars"],
    }
    return "\n".join(lines), data


async def _revert(args: dict[str, Any], ctx: ToolContext) -> tuple[str, Any]:
    model = WorkspaceRevertInput(**_present(args))
    if getattr(ctx, "workspace", None) is None:
        await _emit_patch_refused(ctx, "", "", "", "WORKSPACE_UNBOUND")
        return _patch_refusal("", "WORKSPACE_UNBOUND", "there is no project folder to undo in")
    if not _autonomy_grants_write(ctx):
        await _emit_patch_refused(ctx, "", "", "", "PROFILE_DENIED")
        return _patch_refusal("", "PROFILE_DENIED", "this task isn't set up to make that kind of change")
    existing = getattr(ctx, "_code_workspace", None)
    if existing is None:
        await _emit_patch_refused(ctx, "", "", "", "UNKNOWN_WORKSPACE")
        return _patch_refusal("", "UNKNOWN_WORKSPACE", "there is no change to undo")
    cw, binding, resolved, actor = existing
    try:
        result = patch_mod.revert(resolved, list(model.applied), binding=binding, actor=actor, emitter=ctx.emitter)
    except PatchError as exc:
        await _emit_patch_refused(ctx, cw.id, "", "", exc.code)
        return _patch_refusal(cw.id, exc.code, exc.reason)
    await emit_code_event(ctx, "code.reverted", {"workspaceId": cw.id, "ops": result["reverted"], "ok": True})
    return (
        f"Undid {result['reverted']} change(s) in "
        f"{'the user\'s real folder' if getattr(ctx, 'direct', False) else 'the task\'s working copy'}.",
        {"status": "ok", "workspaceId": cw.id, "reverted": result["reverted"]},
    )


# ── workspace_docs ──────────────────────────────────────────────────────────


def _tokenize(query: str) -> list[str]:
    """Tokenize EXACTLY as `persistence.search_session_messages` does."""
    return re.findall(r"[A-Za-z0-9]+", query)[:12]


def _rank_docs(ws: ResolvedWorkspace, model: WorkspaceDocsInput) -> tuple[list[dict[str, Any]], bool]:
    tokens = _tokenize(model.query)
    if not tokens:
        return [], False
    phrase = " ".join(tokens).lower()
    lowered = [t.lower() for t in tokens]
    excerpts: list[dict[str, Any]] = []
    total_chars = 0
    truncated = False
    for rel, full in _iter_scan_files(ws, model.path):
        try:
            lines = full.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        step = max(1, _DOCS_WINDOW_LINES)
        for window_start in range(0, len(lines), step):
            window = lines[window_start:window_start + _DOCS_WINDOW_LINES]
            if not window:
                continue
            block = "\n".join(window)
            low = block.lower()
            frequency = sum(low.count(token) for token in lowered)
            if frequency == 0 and phrase not in low:
                continue
            phrase_bonus = 100 if phrase and phrase in low else 0
            score = phrase_bonus + frequency
            excerpt = {
                "path": rel,
                "startLine": window_start + 1,
                "endLine": window_start + len(window),
                "text": block,
                "score": score,
            }
            excerpts.append(excerpt)
    # Deterministic ranking: score desc, then path, then start line.
    excerpts.sort(key=lambda e: (-e["score"], e["path"], e["startLine"]))
    selected: list[dict[str, Any]] = []
    for excerpt in excerpts:
        if len(selected) >= model.maxResults:
            truncated = True
            break
        if total_chars + len(excerpt["text"]) > model.maxChars:
            truncated = True
            break
        total_chars += len(excerpt["text"])
        selected.append(excerpt)
    return selected, truncated


async def _docs(args: dict[str, Any], ctx: ToolContext) -> tuple[str, Any]:
    ws: Optional[ResolvedWorkspace] = getattr(ctx, "workspace", None)
    if ws is None:
        await emit_code_event(ctx, "code.refused", {"tool": "workspace_docs", "code": "WORKSPACE_UNAVAILABLE"})
        return _no_workspace("workspace_docs")
    model = WorkspaceDocsInput(**_present(args))
    try:
        selected, truncated = _rank_docs(ws, model)
    except WorkspaceDenial as denial:
        await emit_code_event(ctx, "code.refused", {"tool": "workspace_docs", "code": denial.code, "path": denial.path})
        return _refusal("workspace_docs", denial)
    lines = [f"{len(selected)} passage(s) for {model.query!r} (engine: scan):"]
    for excerpt in selected:
        lines.append(f"--- {excerpt['path']}:{excerpt['startLine']}-{excerpt['endLine']} ---")
        lines.append(excerpt["text"])
    if truncated:
        lines.append("[more passages exist — narrow the question or raise the limit]")
    await emit_code_event(ctx, "code.docs", {
        "query": model.query, "results": len(selected), "engine": "scan",
    })
    return "\n".join(lines), {
        "status": "ok",
        "query": model.query,
        "engine": "scan",
        "results": selected,
        "count": len(selected),
        "truncated": truncated,
    }

# Phase 7 (`delegate`) is registered below; its handler lives in
# `harness/delegation_tools.py` so this module stays under its size ceiling.


# ── Registration ────────────────────────────────────────────────────────────

_CODE_TOOL_NAMES = (
    "workspace_status",
    "workspace_list",
    "workspace_search",
    "workspace_read",
    "workspace_docs",
    "workspace_patch_apply",
    "workspace_diff",
    "workspace_revert",
    "command_run",
    "terminal_run",
    "shell_run", "shell_spawn", "process_output", "process_kill", "process_list",
    "code_commit",
    "code_push",
    "code_open_pr",
    "code_deploy",
    "delegate",
    "analysis_run",
    "scaffold_kit",
    "code_rename_symbol",
)


def _definition(
    name: str, description: str, handler: Any, timeout_ms: int,
    *, permission: str = "read", risk: str = "low", effect: Optional[str] = None,
    deprecated: bool = False, replacement: Optional[str] = None,
) -> ToolDefinition:
    return ToolDefinition(
        name=name,
        description=description,
        input_schema=_INPUT_SCHEMAS[name],
        handler=handler,
        permission=permission,
        risk=risk,
        timeout_ms=timeout_ms,
        effect=effect,
        deprecated=deprecated,
        replacement=replacement,
        parallel_safe=(permission == "read" and name.startswith("workspace_")),
    )


def register_code_tools(registry: ToolRegistry) -> list[str]:
    """Register the code tools. Returns the names added."""
    # Imported lazily: command_tools imports helpers from this module, so a
    # module-level import here would be circular.
    from .command_tools import command_run  # noqa: PLC0415
    from .terminal_tools import terminal_run, terminal_schema  # noqa: PLC0415
    # The fs subsystem's tools are registered first; handlers build a service lazily.
    from .edit_tools import register_edit_tools  # noqa: PLC0415
    from .fs_tools import register_fs_tools  # noqa: PLC0415
    from .todo_tools import register_todo_tools  # noqa: PLC0415
    from .project_memory import register_project_memory_tools  # noqa: PLC0415
    from .checkpoint_tools import register_checkpoint_tools  # noqa: PLC0415

    fs_names = register_fs_tools(registry)
    register_todo_tools(registry)
    checkpoint_names = register_checkpoint_tools(registry)
    # Round 3 / W3 (ADR-H282): the ONE text-edit tool.
    register_edit_tools(registry)
    # Phase 22 / S1: the project's own rules, read-only.
    register_project_memory_tools(registry)

    _INPUT_SCHEMAS["terminal_run"] = terminal_schema()
    from .shell_tools import (  # noqa: PLC0415
        process_kill, process_list, process_output,
        shell_run, shell_schema, shell_spawn, spawn_schema, pid_schema,
    )

    _INPUT_SCHEMAS["shell_run"] = shell_schema()
    _INPUT_SCHEMAS["shell_spawn"] = spawn_schema()
    _INPUT_SCHEMAS["process_output"] = pid_schema()
    _INPUT_SCHEMAS["process_kill"] = pid_schema()
    _INPUT_SCHEMAS["process_list"] = {"type": "object", "properties": {}, "additionalProperties": False}

    registry.register(_definition(
        "workspace_status",
        "Report which project Eclipse is looking at: its saved revision, current line of "
        "work, and any files that already differ from the last saved version (marked as "
        "pre-existing — this task changes nothing). Read-only.",
        _status, 15_000,    ))
    registry.register(_definition(
        "workspace_list",
        "List a folder of the project, ignore-aware and bounded. Read-only; never opens "
        "or changes file contents.",
        _list, 15_000,    ))
    registry.register(_definition(
        "workspace_search",
        "DEPRECATED - use search_text, which is the same search on a faster engine. "
        "Search the project's file contents with a regular expression; every hit is "
        "reported as path:line and the answer names the engine that produced it. "
        "Bounded by matches, files, bytes and a wall-clock deadline; a deadline "
        "yields partial results marked truncated. Read-only.",
        _search, 20_000,
        deprecated=True, replacement="search_text",
    ))
    registry.register(_definition(
        "workspace_read",
        "Read a file with line numbers. Supports a continuation offset (startLine / "
        "nextStartLine). Refuses binary files, and redacts secret-shaped lines. Read-only.",
        _read, 15_000,    ))
    registry.register(_definition(
        "workspace_docs",
        "Find ranked passages in the project's notes and documentation (docs/**). Returns "
        "bounded excerpts with their path:line range. A bounded scan per call — no index "
        "is built. Read-only.",
        _docs, 25_000,    ))
    registry.register(_definition(
        "workspace_patch_apply",
        "Author a change in the project (create/replace/delete/move files). Whether it lands "
        "in the user's real folder or a disposable copy depends on the task's setup — call "
        "workspace_status to see which. The whole change is validated before any byte is "
        "written — a refusal applies nothing. Requires the run's editing profile.",
        _patch_apply, 60_000, permission="write", risk="medium", effect="write",
        deprecated=True, replacement="code_edit",
    ))
    registry.register(_definition(
        "workspace_diff",
        "Show the changes made inside the disposable copy of the project: a unified diff "
        "plus the added/modified/deleted files and a digest. Read-only.",
        _diff, 30_000,    ))
    registry.register(_definition(
        "workspace_revert",
        "Undo a change previously authored in the disposable copy of the project, newest "
        "operation first. Refuses over a changed base.",
        _revert, 60_000, permission="write", risk="medium", effect="write",
    ))
    registry.register(_definition(
        "command_run",
        "Run one allowlisted check (typecheck, lint, unit tests, python tests, build, "
        "format check, read-only git) inside the project's own copy. The command is chosen "
        "from a fixed table — no shell, no arbitrary program — and every run is recorded. "
        "Refused unless the session may run checks.",
        command_run, 120_000, permission="write", risk="medium", effect="external_action",
    ))
    # Phase D: generic terminal. Runs an arbitrary command WITHOUT a shell; safe
    # tools run directly, others require a per-command single-use approval, and
    # destructive commands are refused. Effectful, so the authorizer gates it.
    registry.register(_definition(
        "terminal_run",
        "Run a command in the project folder (no shell). Use for building, running "
        "scripts, tests, or anything not covered by the fixed checks. Safe tools run "
        "directly; other commands ask the user first; destructive commands are refused.",
        terminal_run, 900_000, permission="write", risk="high", effect="external_action",
    ))
    # Real shell through the host shell (PowerShell/CMD/bash): pipes and
    # redirection work; classified allow/ask/deny, ask needs an approval.
    registry.register(_definition(
        "shell_run",
        "Run a command through the real system shell (PowerShell/CMD/bash) in the project folder. "
        "Pipes and redirection work. Safe commands run directly; others ask the user first; destructive ones are refused.",
        shell_run, 900_000, permission="write", risk="high", effect="external_action",
    ))
    registry.register(_definition(
        "shell_spawn",
        "Start a long-running command in the background through the real shell. Returns a pid; inspect with process_output, stop with process_kill.",
        shell_spawn, 60_000, permission="write", risk="high", effect="external_action",
    ))
    registry.register(_definition(
        "process_output",
        "Read the current stdout/stderr and status of a background process started by shell_spawn. Read-only.",
        process_output, 30_000,
    ))
    registry.register(_definition(
        "process_kill",
        "Stop a background process started by shell_spawn.",
        process_kill, 30_000, permission="write", risk="medium", effect="external_action",
    ))
    registry.register(_definition(
        "process_list",
        "List background processes started by shell_spawn and their states. Read-only.",
        process_list, 30_000,
    ))
    # The external actions (Phase 5): each external_action, each requiring a
    # single-use, workspace-bound operator approval resolved by actionKind.
    from .action_tools import _make_handler, action_definition_args  # noqa: PLC0415

    for spec in action_definition_args():
        registry.register(ToolDefinition(
            name=spec["name"],
            description=spec["description"] + " Requires the app owner's approval for each action.",
            input_schema=_INPUT_SCHEMAS["code_action"],
            handler=_make_handler(spec["action"]),
            permission="write", risk="medium", timeout_ms=60_000,
            effect="external_action",
            effect_metadata={"actionKind": spec["action"]},
        ))
    # Phase 7: bounded fan-out. `read` at the registry seam (each child enforces
    # its own policy). The handler lives in `harness/delegation_tools.py`.
    from ..delegation_tools import delegate_handler  # noqa: PLC0415

    registry.register(ToolDefinition(
        name="delegate",
        description=(
            "Work on several INDEPENDENT sub-questions at once and combine the results. "
            "Use this only when the parts do not need each other's output (e.g. review five "
            "separate companies). Each part runs as a normal run with this run as its parent; "
            "a part's claim is repeated only when it can be re-checked, and anything that "
            "cannot be confirmed is reported as unconfirmed."
        ),
        input_schema=_INPUT_SCHEMAS["delegate"],
        handler=delegate_handler,
        permission="read", risk="low", timeout_ms=600_000,
    ))
    # W7: the durable background lifecycle over the ONE Fabric. `delegate`
    # remains the synchronous wrapper; the flag gates the async surface.
    from ..delegation_async_tools import register_delegation_async_tools  # noqa: PLC0415

    async_names = register_delegation_async_tools(registry)
    from ..analysis_tools import analysis_run, recipe_catalogue_text as _RECIPE_CATALOG  # noqa: PLC0415

    registry.register(ToolDefinition(
        name="analysis_run",
        description=(
            "Work something out from data with a short calculation — a return, a drawdown, "
            # Round 3 / W10 (ADR-H298): this was the FUNCTION OBJECT, not its
            # result - so the tool that runs analyses advertised
            # `<function recipe_catalogue_text at 0x...>` to the model instead
            # of the recipes it can actually run. The description is now the
            # catalogue, and it is byte-stable, which is what makes the
            # published manifest comparable.
            f"a correlation, a reconciliation, a what-if. Ready-made recipes: {_RECIPE_CATALOG()}. Otherwise "
            "give a short program that reads `inputs` and puts its answer in `result`. The "
            "result is always labelled as worked out by Eclipse, never as data from a source. "
            "If a needed input is missing, ask — never invent it."
        ),
        input_schema=_INPUT_SCHEMAS["analysis"],
        handler=analysis_run,
        permission="read", risk="low", timeout_ms=180_000,
    ))
    # Phase 9: finance-application scaffold kits. The handler lives in
    # `harness/scaffold_tools.py`; it writes into the task's disposable workspace
    # and runs the kit's own check.
    from ..scaffold_tools import scaffold_kit  # noqa: PLC0415

    registry.register(ToolDefinition(
        name="scaffold_kit",
        description=(
            "Build a ready-made finance tool from a short description of the job — a new data "
            "source, a ratio, a backtest, a chart indicator, a portfolio analysis, an import job, "
            "a repeatable research routine. Eclipse writes the files into the task's own copy, "
            "runs the check that proves it works, and keeps it for you to review. Nothing turns "
            "itself on."
        ),
        input_schema=_INPUT_SCHEMAS["scaffold"],
        handler=scaffold_kit,
        permission="write", risk="medium", timeout_ms=600_000, effect="write",
    ))
    from ..capabilities.tools import register_capability_tools  # noqa: PLC0415
    register_capability_tools(registry)
    # The Code Intelligence & Understanding Engine (CIUE). Its read tools are
    # always available; the one write tool (`code_rename_symbol`) is admitted by
    # the effect authorizer and applies to the disposable workspace. Handler:
    # `harness/code/intel_tools.py`; manager: `harness/code/intel/`.
    from .intel_tools import register_intel_tools  # noqa: PLC0415

    intel_names = register_intel_tools(registry)
    return (
        list(_CODE_TOOL_NAMES) + list(async_names) + list(checkpoint_names)
        + list(fs_names) + list(intel_names)
    )


__all__ = [
    "REDACT_MARKER",
    "register_code_tools",
    "emit_code_event",
    "WorkspaceStatusInput",
    "WorkspaceListInput",
    "WorkspaceSearchInput",
    "WorkspaceReadInput",
    "WorkspaceDocsInput",
    "PatchApplyInput",
    "WorkspaceDiffInput",
    "WorkspaceRevertInput",
    "CommandRunInput",
]
