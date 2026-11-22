"""The CIUE agent tools — the `code_*` surface the model actually calls.

Each handler is thin: resolve the workspace, get the process-wide CIUE runtime,
build a `CodeIntelligenceManager` for the run's workspace, call ONE manager method,
map a structured `IntelError` to a readable refusal and return `(text, data)`
exactly like every other code tool. The `data` payload is the normalized Unified
Code Model record the front door reads; `text` is a compact, bounded JSON view for
the model.

The write tool (`code_rename_symbol`) is admitted by the effect authorizer and
applies to the disposable workspace (or the chosen folder in direct mode), exactly
like `workspace_patch_apply`: the semantic edit is computed against the read-only
root and rebased onto the write root.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field

from ..tools import ToolContext, ToolDefinition, ToolRegistry
from .intel.errors import IntelError, NO_WORKSPACE
from .intel.runtime import get_code_intel_runtime
from .tools import _ensure_code_workspace, _present, emit_code_event

# ── input models ─────────────────────────────────────────────────────────────


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FindSymbolInput(_Base):
    name: str = Field(..., min_length=1, max_length=200)
    file: Optional[str] = Field(None, max_length=1024)
    kind: Optional[str] = Field(None, max_length=32)
    limit: int = Field(20, ge=1, le=200)


class SearchSymbolsInput(_Base):
    query: str = Field(..., min_length=1, max_length=200)
    kind: Optional[str] = Field(None, max_length=32)
    language: Optional[str] = Field(None, max_length=32)
    limit: int = Field(100, ge=1, le=500)


class DefinitionInput(_Base):
    name: str = Field(..., min_length=1, max_length=200)
    file: Optional[str] = Field(None, max_length=1024)
    line: Optional[int] = Field(None, ge=1)
    column: Optional[int] = Field(None, ge=1)


class ReferencesInput(_Base):
    name: str = Field(..., min_length=1, max_length=200)
    file: Optional[str] = Field(None, max_length=1024)
    limit: int = Field(200, ge=1, le=500)


class HoverInput(_Base):
    file: str = Field(..., min_length=1, max_length=1024)
    line: int = Field(..., ge=1)
    column: int = Field(1, ge=1)


class TypeInput(_Base):
    name: str = Field(..., min_length=1, max_length=200)
    file: Optional[str] = Field(None, max_length=1024)


class DiagnosticsInput(_Base):
    file: Optional[str] = Field(None, max_length=1024)
    severity: Optional[str] = Field(None, pattern="^(error|warning|information|hint)$")


class CallInput(_Base):
    name: str = Field(..., min_length=1, max_length=200)
    file: Optional[str] = Field(None, max_length=1024)


class CallHierarchyInput(_Base):
    name: str = Field(..., min_length=1, max_length=200)
    file: Optional[str] = Field(None, max_length=1024)
    direction: str = Field("both", pattern="^(both|in|out|incoming|outgoing)$")


class ImplementationsInput(_Base):
    name: str = Field(..., min_length=1, max_length=200)
    file: Optional[str] = Field(None, max_length=1024)


class FileInput(_Base):
    path: str = Field(..., min_length=1, max_length=1024)


class DependencyInput(_Base):
    file: Optional[str] = Field(None, max_length=1024)
    symbol: Optional[str] = Field(None, max_length=200)


class DependencyGraphInput(_Base):
    maxNodes: int = Field(500, ge=1, le=5000)  # noqa: N815


class RenameInput(_Base):
    name: str = Field(..., min_length=1, max_length=200)
    newName: str = Field(..., min_length=1, max_length=200)  # noqa: N815
    file: Optional[str] = Field(None, max_length=1024)
    dryRun: bool = True  # noqa: N815


class RelevantContextInput(_Base):
    query: str = Field(..., min_length=1, max_length=2000)
    maxFiles: int = Field(12, ge=1, le=50)  # noqa: N815
    maxSymbols: int = Field(40, ge=1, le=200)  # noqa: N815


class IndexInput(_Base):
    force: bool = False


class RelatedTestsInput(_Base):
    symbol: Optional[str] = Field(None, max_length=200)
    file: Optional[str] = Field(None, max_length=1024)


_STR = {"type": "string"}
_INT = {"type": "integer"}
_BOOL = {"type": "boolean"}


def _obj(properties: dict[str, Any], required: Optional[list[str]] = None) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": "object", "properties": properties, "additionalProperties": False}
    if required:
        schema["required"] = required
    return schema


_INPUT_SCHEMAS: dict[str, dict[str, Any]] = {
    "code_get_definition": _obj({
        "name": {**_STR, "description": "The symbol name to locate the definition of."},
        "file": {**_STR, "description": "Optional file that disambiguates a duplicate name."},
        "line": {**_INT}, "column": {**_INT},
    }, ["name"]),
    "code_find_references": _obj({
        "name": {**_STR, "description": "The symbol name to find usages of."},
        "file": {**_STR}, "limit": {**_INT},
    }, ["name"]),
    "code_find_symbol": _obj({
        "name": {**_STR, "description": "A symbol name to find (exact first, then fuzzy)."},
        "file": {**_STR}, "kind": {**_STR}, "limit": {**_INT},
    }, ["name"]),
    "code_search_symbols": _obj({
        "query": {**_STR, "description": "A name fragment to search for."},
        "kind": {**_STR}, "language": {**_STR}, "limit": {**_INT},
    }, ["query"]),
    "code_get_hover": _obj({
        "file": {**_STR}, "line": {**_INT}, "column": {**_INT},
    }, ["file", "line"]),
    "code_get_type": _obj({"name": {**_STR}, "file": {**_STR}}, ["name"]),
    "code_get_diagnostics": _obj({
        "file": {**_STR, "description": "One file, or omit for the whole workspace."},
        "severity": {**_STR, "enum": ["error", "warning", "information", "hint"]},
    }),
    "code_get_document_symbols": _obj({"path": {**_STR}}, ["path"]),
    "code_get_callers": _obj({"name": {**_STR}, "file": {**_STR}}, ["name"]),
    "code_get_callees": _obj({"name": {**_STR}, "file": {**_STR}}, ["name"]),
    "code_get_call_hierarchy": _obj({
        "name": {**_STR}, "file": {**_STR},
        "direction": {**_STR, "enum": ["both", "in", "out", "incoming", "outgoing"]},
    }, ["name"]),
    "code_get_implementations": _obj({"name": {**_STR}, "file": {**_STR}}, ["name"]),
    "code_get_imports": _obj({"path": {**_STR}}, ["path"]),
    "code_get_exports": _obj({"path": {**_STR}}, ["path"]),
    "code_get_dependencies": _obj({"file": {**_STR}, "symbol": {**_STR}}),
    "code_get_dependents": _obj({"file": {**_STR}, "symbol": {**_STR}}),
    "code_get_project_structure": _obj({}),
    "code_get_dependency_graph": _obj({"maxNodes": {**_INT}}),
    "code_parse_file": _obj({"path": {**_STR}}, ["path"]),
    "code_analyze_file": _obj({"path": {**_STR}}, ["path"]),
    "code_rename_symbol": _obj({
        "name": {**_STR, "description": "The current symbol name."},
        "newName": {**_STR, "description": "The new name."},
        "file": {**_STR}, "dryRun": {**_BOOL, "description": "Preview only (default true)."},
    }, ["name", "newName"]),
    "code_organize_imports": _obj({"path": {**_STR}}),
    "code_get_relevant_context": _obj({
        "query": {**_STR, "description": "The task or question, in plain language."},
        "maxFiles": {**_INT}, "maxSymbols": {**_INT},
    }, ["query"]),
    "code_index_workspace": _obj({"force": {**_BOOL, "description": "Rebuild the index from scratch."}}),
    "code_intel_status": _obj({}),
    "code_find_related_tests": _obj({"symbol": {**_STR}, "file": {**_STR}}),
}


# ── helpers ──────────────────────────────────────────────────────────────────


def _runtime(ctx: ToolContext) -> Any:
    override = getattr(ctx, "code_intel_runtime", None)
    if override is not None:
        return override
    return get_code_intel_runtime()


def _manager(ctx: ToolContext):
    ws = getattr(ctx, "workspace", None)
    if ws is None:
        raise IntelError(
            NO_WORKSPACE, "There is no project folder available to look through.",
        )
    return _runtime(ctx).manager_for(ws)


def _is_fresh(ctx: ToolContext, workspace: Any) -> bool:
    """Whether the background indexer already keeps this workspace current.

    The runtime owns freshness: its per-workspace watcher + incremental indexer
    keep the shared index up to date (see `CodeIntelRuntime.is_fresh`). When it
    says fresh, a read query must NOT trigger its own full synchronous walk —
    that is what wedged the runtime (a minutes-long index on the event loop).
    A DISABLED runtime has no background indexer, so it never counts as fresh and
    the on-demand refresh still runs (off the event loop).
    """
    try:
        runtime = _runtime(ctx)
        if not getattr(runtime, "enabled", True):
            return False
        return bool(runtime.is_fresh(workspace))
    except Exception:  # noqa: BLE001 - unknown freshness is treated as stale
        return False


def _ensure_fresh(manager: Any, ctx: ToolContext) -> None:
    """Refresh the index only when the runtime does not already own freshness.

    Never called on the event loop directly — callers run it via
    `asyncio.to_thread` so a heavy index cannot stall health checks or runs.
    """
    if not _is_fresh(ctx, manager.workspace):
        manager._ensure_fresh()


def _refusal(tool: str, error: IntelError) -> tuple[str, Any]:
    text = f"REFUSED ({error.code}): {error.reason}"
    if error.recovery:
        text += f" {error.recovery}"
    return text, error.to_dict()


def _payload(payload: dict[str, Any], *, summary: str) -> tuple[str, Any]:
    """A compact JSON text view plus the structured record for the front door."""
    try:
        text = summary + "\n" + json.dumps(payload, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        text = summary
    return text, payload


async def _guard(ctx: ToolContext, tool: str, run: Any) -> tuple[str, Any]:
    try:
        return await run()
    except IntelError as error:
        await emit_code_event(ctx, "code.intel.refused", {"tool": tool, "code": error.code, "path": error.path})
        return _refusal(tool, error)


async def _call(ctx: ToolContext, tool: str, method: str, summary: str, **kwargs: Any) -> tuple[str, Any]:
    def _work() -> Any:
        # Acquiring the manager (per-workspace watcher setup) and the query are
        # both synchronous and potentially heavy. Run BOTH off the event loop: a
        # blocking call on the loop cannot be cancelled by the tool timeout and
        # freezes the runtime's /health and every concurrent run.
        manager = _manager(ctx)
        _ensure_fresh(manager, ctx)
        return getattr(manager, method)(**kwargs)

    async def _run() -> tuple[str, Any]:
        result = await asyncio.to_thread(_work)
        await emit_code_event(ctx, f"code.intel.{method}", {"tool": tool})
        return _payload(result, summary=_summary_text(summary, result))

    return await _guard(ctx, tool, _run)


def _summary_text(prefix: str, result: dict[str, Any]) -> str:
    count = result.get("count")
    engine = result.get("engine")
    suffix = f" (engine: {engine})" if engine else ""
    if count is not None:
        return f"{prefix}: {count} result(s){suffix}."
    return f"{prefix}{suffix}."


# ── handlers ─────────────────────────────────────────────────────────────────


async def _get_definition(args: dict[str, Any], ctx: ToolContext) -> tuple[str, Any]:
    model = DefinitionInput(**_present(args))
    return await _call(ctx, "code_get_definition", "get_definition", "Definition found",
                       name=model.name, file=model.file, line=model.line, column=model.column)


async def _find_references(args: dict[str, Any], ctx: ToolContext) -> tuple[str, Any]:
    model = ReferencesInput(**_present(args))
    return await _call(ctx, "code_find_references", "find_references", "References found",
                       name=model.name, file=model.file, limit=model.limit)


async def _find_symbol(args: dict[str, Any], ctx: ToolContext) -> tuple[str, Any]:
    model = FindSymbolInput(**_present(args))
    return await _call(ctx, "code_find_symbol", "find_symbol", "Symbols found",
                       name=model.name, file=model.file, kind=model.kind, limit=model.limit)


async def _search_symbols(args: dict[str, Any], ctx: ToolContext) -> tuple[str, Any]:
    model = SearchSymbolsInput(**_present(args))
    return await _call(ctx, "code_search_symbols", "search_symbols", "Symbols matched",
                       query=model.query, kind=model.kind, language=model.language, limit=model.limit)


async def _get_hover(args: dict[str, Any], ctx: ToolContext) -> tuple[str, Any]:
    model = HoverInput(**_present(args))
    return await _call(ctx, "code_get_hover", "get_hover", "Hover",
                       file=model.file, line=model.line, column=model.column)


async def _get_type(args: dict[str, Any], ctx: ToolContext) -> tuple[str, Any]:
    model = TypeInput(**_present(args))
    return await _call(ctx, "code_get_type", "get_type_information", "Type information",
                       name=model.name, file=model.file)


async def _get_diagnostics(args: dict[str, Any], ctx: ToolContext) -> tuple[str, Any]:
    model = DiagnosticsInput(**_present(args))
    return await _call(ctx, "code_get_diagnostics", "get_diagnostics", "Diagnostics",
                       file=model.file, severity=model.severity)


async def _get_document_symbols(args: dict[str, Any], ctx: ToolContext) -> tuple[str, Any]:
    model = FileInput(**_present(args))
    return await _call(ctx, "code_get_document_symbols", "get_document_symbols", "Document symbols",
                       rel_path=model.path)


async def _get_callers(args: dict[str, Any], ctx: ToolContext) -> tuple[str, Any]:
    model = CallInput(**_present(args))
    return await _call(ctx, "code_get_callers", "get_incoming_calls", "Callers",
                       name=model.name, file=model.file)


async def _get_callees(args: dict[str, Any], ctx: ToolContext) -> tuple[str, Any]:
    model = CallInput(**_present(args))
    return await _call(ctx, "code_get_callees", "get_outgoing_calls", "Callees",
                       name=model.name, file=model.file)


async def _get_call_hierarchy(args: dict[str, Any], ctx: ToolContext) -> tuple[str, Any]:
    model = CallHierarchyInput(**_present(args))
    return await _call(ctx, "code_get_call_hierarchy", "get_call_hierarchy", "Call hierarchy",
                       name=model.name, file=model.file, direction=model.direction)


async def _get_implementations(args: dict[str, Any], ctx: ToolContext) -> tuple[str, Any]:
    model = ImplementationsInput(**_present(args))
    return await _call(ctx, "code_get_implementations", "get_implementations", "Implementations",
                       name=model.name, file=model.file)


async def _get_imports(args: dict[str, Any], ctx: ToolContext) -> tuple[str, Any]:
    model = FileInput(**_present(args))
    return await _call(ctx, "code_get_imports", "get_imports", "Imports", rel_path=model.path)


async def _get_exports(args: dict[str, Any], ctx: ToolContext) -> tuple[str, Any]:
    model = FileInput(**_present(args))
    return await _call(ctx, "code_get_exports", "get_exports", "Exports", rel_path=model.path)


async def _get_dependencies(args: dict[str, Any], ctx: ToolContext) -> tuple[str, Any]:
    model = DependencyInput(**_present(args))
    return await _call(ctx, "code_get_dependencies", "get_dependencies", "Dependencies",
                       file=model.file, symbol=model.symbol)


async def _get_dependents(args: dict[str, Any], ctx: ToolContext) -> tuple[str, Any]:
    model = DependencyInput(**_present(args))
    return await _call(ctx, "code_get_dependents", "get_dependents", "Dependents",
                       file=model.file, symbol=model.symbol)


async def _get_project_structure(args: dict[str, Any], ctx: ToolContext) -> tuple[str, Any]:
    return await _call(ctx, "code_get_project_structure", "get_project_structure", "Project structure")


async def _get_dependency_graph(args: dict[str, Any], ctx: ToolContext) -> tuple[str, Any]:
    model = DependencyGraphInput(**_present(args))
    return await _call(ctx, "code_get_dependency_graph", "get_dependency_graph", "Dependency graph",
                       max_nodes=model.maxNodes)


async def _parse_file(args: dict[str, Any], ctx: ToolContext) -> tuple[str, Any]:
    model = FileInput(**_present(args))
    return await _call(ctx, "code_parse_file", "parse_file", "Parsed", rel_path=model.path)


async def _analyze_file(args: dict[str, Any], ctx: ToolContext) -> tuple[str, Any]:
    model = FileInput(**_present(args))
    return await _call(ctx, "code_analyze_file", "analyze_file", "Analyzed", rel_path=model.path)


async def _organize_imports(args: dict[str, Any], ctx: ToolContext) -> tuple[str, Any]:
    model = FileInput(**_present(args))
    return await _call(ctx, "code_organize_imports", "organize_imports", "Organized imports",
                       rel_path=model.path)


async def _get_relevant_context(args: dict[str, Any], ctx: ToolContext) -> tuple[str, Any]:
    model = RelevantContextInput(**_present(args))
    return await _call(ctx, "code_get_relevant_context", "get_relevant_context", "Relevant context",
                       query=model.query, max_files=model.maxFiles, max_symbols=model.maxSymbols)


async def _index_workspace(args: dict[str, Any], ctx: ToolContext) -> tuple[str, Any]:
    model = IndexInput(**_present(args))
    return await _call(ctx, "code_index_workspace", "index_workspace", "Indexed", force=model.force)


async def _find_related_tests(args: dict[str, Any], ctx: ToolContext) -> tuple[str, Any]:
    model = RelatedTestsInput(**_present(args))
    return await _call(ctx, "code_find_related_tests", "find_related_tests", "Related tests",
                       symbol=model.symbol, file=model.file)


async def _intel_status(args: dict[str, Any], ctx: ToolContext) -> tuple[str, Any]:
    def _collect() -> dict[str, Any]:
        runtime = _runtime(ctx)
        status = runtime.status()
        ws = getattr(ctx, "workspace", None)
        if ws is not None:
            manager = runtime.manager_for(ws)
            status["workspace"] = manager.index_status()
            status["project"] = manager.detect_project()
            status["servers"] = manager.language_server_status()
        return status

    async def _run() -> tuple[str, Any]:
        status = await asyncio.to_thread(_collect)
        return _payload(status, summary="Code intelligence status.")

    return await _guard(ctx, "code_intel_status", _run)


async def _rename_symbol(args: dict[str, Any], ctx: ToolContext) -> tuple[str, Any]:
    model = RenameInput(**_present(args))

    async def _run() -> tuple[str, Any]:
        manager = await asyncio.to_thread(_manager, ctx)
        await asyncio.to_thread(_ensure_fresh, manager, ctx)
        result = await asyncio.to_thread(
            lambda: manager.rename_symbol(model.name, model.newName, file=model.file, dry_run=model.dryRun)
        )
        preview = result.to_dict()
        if model.dryRun or not result.files:
            await emit_code_event(ctx, "code.intel.rename.preview", {
                "symbol": model.name, "files": len(result.files), "engine": result.engine,
            })
            return _payload(
                {"status": "ok", "applied": False, "preview": preview},
                summary=f"Rename preview for {model.name} → {model.newName} (engine: {result.engine}).",
            )
        # Apply the semantic edit to the disposable workspace (or direct folder).
        if not _autonomy_grants_write(ctx):
            from . import policy as _policy

            if "code.write" not in _policy.code_grants(str(getattr(ctx, "autonomy", None))):
                raise IntelError("EDIT_CONFLICT", "This task isn't set up to make that kind of change.")
        cw, _binding, _resolved, _actor = _ensure_code_workspace(ctx)
        applied = await asyncio.to_thread(
            lambda: manager.apply_workspace_edit(result.files, write_root=cw.root, dry_run=False)
        )
        await emit_code_event(ctx, "code.intel.rename.applied", {
            "symbol": model.name, "newName": model.newName, "engine": result.engine,
            "filesChanged": len(applied.get("applied", [])),
        })
        payload = {
            "status": "ok", "applied": True, "engine": result.engine,
            "files": applied.get("applied", []), "conflicts": applied.get("conflicts", []),
            "workspaceId": cw.id,
        }
        return _payload(payload, summary=f"Renamed {model.name} → {model.newName} (engine: {result.engine}).")

    return await _guard(ctx, "code_rename_symbol", _run)


def _autonomy_grants_write(ctx: ToolContext) -> bool:
    from . import policy as _policy

    autonomy = getattr(ctx, "autonomy", None)
    return bool(autonomy and "code.write" in _policy.code_grants(str(autonomy)))


# ── registration ─────────────────────────────────────────────────────────────

_READ_TOOLS = (
    "code_get_definition", "code_find_references", "code_find_symbol",
    "code_search_symbols", "code_get_hover", "code_get_type",
    "code_get_diagnostics", "code_get_document_symbols", "code_get_callers",
    "code_get_callees", "code_get_call_hierarchy", "code_get_implementations",
    "code_get_imports", "code_get_exports", "code_get_dependencies",
    "code_get_dependents", "code_get_project_structure", "code_get_dependency_graph",
    "code_parse_file", "code_analyze_file", "code_organize_imports",
    "code_get_relevant_context", "code_index_workspace", "code_intel_status",
    "code_find_related_tests",
)
_WRITE_TOOLS = ("code_rename_symbol",)

_HANDLERS: dict[str, Any] = {
    "code_get_definition": _get_definition,
    "code_find_references": _find_references,
    "code_find_symbol": _find_symbol,
    "code_search_symbols": _search_symbols,
    "code_get_hover": _get_hover,
    "code_get_type": _get_type,
    "code_get_diagnostics": _get_diagnostics,
    "code_get_document_symbols": _get_document_symbols,
    "code_get_callers": _get_callers,
    "code_get_callees": _get_callees,
    "code_get_call_hierarchy": _get_call_hierarchy,
    "code_get_implementations": _get_implementations,
    "code_get_imports": _get_imports,
    "code_get_exports": _get_exports,
    "code_get_dependencies": _get_dependencies,
    "code_get_dependents": _get_dependents,
    "code_get_project_structure": _get_project_structure,
    "code_get_dependency_graph": _get_dependency_graph,
    "code_parse_file": _parse_file,
    "code_analyze_file": _analyze_file,
    "code_rename_symbol": _rename_symbol,
    "code_organize_imports": _organize_imports,
    "code_get_relevant_context": _get_relevant_context,
    "code_index_workspace": _index_workspace,
    "code_intel_status": _intel_status,
    "code_find_related_tests": _find_related_tests,
}

_DESCRIPTIONS: dict[str, str] = {
    "code_get_definition": "Where a symbol is defined: file, line, column, signature and the surrounding line. Semantic (language server) or structural (index).",
    "code_find_references": "Every place a symbol is used, each marked as a definition or a usage.",
    "code_find_symbol": "Find a symbol by name (exact first, then fuzzy), optionally scoped to a file.",
    "code_search_symbols": "Search functions/classes/types/etc. by name fragment, kind or language.",
    "code_get_hover": "The type/documentation a language tool shows at a position in a file.",
    "code_get_type": "The declared signature and type of a symbol.",
    "code_get_diagnostics": "Errors, warnings and hints for one file or the whole workspace (semantic + structural).",
    "code_get_document_symbols": "The symbols declared in one file, nested by container.",
    "code_get_callers": "The functions that call a symbol (semantic call hierarchy or the call graph).",
    "code_get_callees": "The functions a symbol calls.",
    "code_get_call_hierarchy": "Both directions of a symbol's call relationships.",
    "code_get_implementations": "Types/classes that implement or extend a symbol.",
    "code_get_imports": "The imports of one file, with resolved targets where local.",
    "code_get_exports": "The exported symbols of one file.",
    "code_get_dependencies": "The files a file or symbol depends on (with the transitive closure).",
    "code_get_dependents": "The files that depend on a file or symbol.",
    "code_get_project_structure": "The project's languages, frameworks, entry points, folders and checkout state.",
    "code_get_dependency_graph": "A bounded file→file dependency graph for the project.",
    "code_parse_file": "The structural parse of one file: symbols, imports, exports, routes, models.",
    "code_analyze_file": "A file's symbols, imports, exports, routes, models and diagnostics in one call.",
    "code_rename_symbol": "Rename a symbol (semantic WorkspaceEdit, or a preview-first structural edit). Previews by default.",
    "code_organize_imports": "Report the import state of a file. Conservative: it never reorders without a language server.",
    "code_get_relevant_context": "The focused files, symbols, call paths and tests relevant to a request — never the whole repository.",
    "code_index_workspace": "Build/refresh the dedicated code index incrementally.",
    "code_intel_status": "What CIUE currently has: index size, project shape, running language tools.",
    "code_find_related_tests": "The tests likely related to a symbol or file (naming + reference match).",
}


def _definition(name: str) -> ToolDefinition:
    write = name in _WRITE_TOOLS
    return ToolDefinition(
        name=name,
        description=_DESCRIPTIONS[name],
        input_schema=_INPUT_SCHEMAS[name],
        handler=_HANDLERS[name],
        permission="write" if write else "read",
        risk="medium" if write else "low",
        timeout_ms=60_000 if write else 30_000,
        effect="write" if write else None,
        # Indexing warms shared state; every other read is independent.
        parallel_safe=(not write and name != "code_index_workspace"),
    )


def register_intel_tools(registry: ToolRegistry) -> list[str]:
    """Register the CIUE tools. Returns the names added."""
    names = list(_READ_TOOLS) + list(_WRITE_TOOLS)
    existing = set(registry.names())
    for name in names:
        if name in existing:
            continue
        registry.register(_definition(name))
    return names


__all__ = [
    "INTEL_READ_TOOL_NAMES",
    "INTEL_WRITE_TOOL_NAMES",
    "register_intel_tools",
]

INTEL_READ_TOOL_NAMES = _READ_TOOLS
INTEL_WRITE_TOOL_NAMES = _WRITE_TOOLS
