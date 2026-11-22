"""The Code Intelligence Manager — the ONE orchestration service (CIUE).

Everything the agent asks for flows through this object. It coordinates the LSP
manager, the AST engine, the dedicated code index, the dependency graph, the
diagnostics engine, structural search, the project analyzer and the semantic
editor, and normalizes every answer into the Unified Code Model.

The agent never learns which backend answered — but every result DOES name its
`engine` (`lsp` / `tree-sitter` / `ast` / `index`), because honesty about the
source is required, not hidden. The manager is deterministic, LLM-free, and never
lets a language-server failure crash the runtime: LSP is tried first, then the
structured fallback, in that order.

This object is per-run (it reads a resolved workspace root) but holds only a
reference to process-wide singletons (the index is opened per root; the LSP
manager is process-wide and injected).
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

from ..fs import encoding as fs_encoding
from ..fs import git as fs_git
from ..workspace import ResolvedWorkspace
from .ast import ASTEngine
from .context import CodeContextEngine
from .dependencies import DependencyGraph
from .diagnostics import DiagnosticsEngine
from .editing import (
    FileEdit,
    RenameResult,
    SemanticEditor,
    apply_file_edits,
    organize_imports_fallback,
    parse_workspace_edit,
)
from .errors import IntelError, IntelUnavailable, NOT_FOUND, UNSUPPORTED_LANGUAGE
from .language.detect import detect_language
from .lsp.manager import LSPManager
from .models import CodeDiagnostic, CodeSymbol
from .persistence.index import CodeIndex, open_code_index
from .indexer import IncrementalIndexer
from .project import ProjectAnalyzer, project_id_for
from .search import StructuralSearch

log = logging.getLogger("harness.code.intel.manager")


class CodeIntelligenceManager:
    """The unified code-intelligence surface."""

    def __init__(
        self,
        workspace: ResolvedWorkspace,
        *,
        lsp_manager: Optional[LSPManager] = None,
        ast: Optional[ASTEngine] = None,
        index: Optional[CodeIndex] = None,
        on_event: Optional[Callable[[str, dict[str, Any]], None]] = None,
    ) -> None:
        self.workspace = workspace
        self.root = Path(workspace.root)
        self.project_id = project_id_for(self.root, getattr(workspace, "id", ""))
        self.workspace_id = getattr(workspace, "id", "")
        self._on_event = on_event
        self.lsp = lsp_manager if lsp_manager is not None else LSPManager(on_event=on_event)
        self.index = index if index is not None else open_code_index(self.project_id)
        self.ast = ast or ASTEngine(self.project_id, root=self.root)
        self.analyzer = ProjectAnalyzer(self.root, self.project_id, workspace_id=self.workspace_id)
        self.indexer = IncrementalIndexer(self.root, self.project_id, self.index, ast=self.ast, on_event=on_event)
        self.diagnostics = DiagnosticsEngine(self.index)
        self._deps: Optional[DependencyGraph] = None
        self._search = StructuralSearch(self.index)
        self.editor = SemanticEditor(self.root, self.index)
        self._lock = threading.RLock()
        self._open_docs: set[str] = set()
        self._last_index_ms = 0.0

    # ── freshness (no stale semantic data) ──────────────────────────────────────
    def _ensure_fresh(self, *, max_age_s: float = 30.0, force: bool = False) -> None:
        """Refresh the index incrementally before a query when it may be stale.

        The index is a CACHE, not an authority. A query must never answer from a
        stale symbol set: if the last pass is older than `max_age_s` (or `force`),
        the affected files are re-parsed first — only changed files, never a full
        rebuild. A refresh failure falls through to the existing index rather than
        failing the query.
        """
        now = time.monotonic()
        if not force and self._last_index_ms and (now - self._last_index_ms) < max_age_s:
            return
        try:
            self.indexer.index_workspace()
            self._deps = None
        except Exception as exc:  # noqa: BLE001 - a refresh must not break a query
            self._emit("code.intel.refresh_failed", {"error": str(exc)[:200]})
        finally:
            self._last_index_ms = time.monotonic()

    # ── project / languages ────────────────────────────────────────────────────
    def detect_project(self) -> dict[str, Any]:
        info = self.analyzer.analyze(self.index)
        try:
            self.index.set_project(
                workspace_id=self.workspace_id, root=str(self.root),
                languages=info.languages, frameworks=info.frameworks,
            )
        except Exception:  # noqa: BLE001
            pass
        return info.to_dict()

    def detect_languages(self) -> list[str]:
        return self.analyzer.detect_languages().languages

    def get_project_structure(self) -> dict[str, Any]:
        return self.analyzer.structure(self.index)

    # ── indexing ───────────────────────────────────────────────────────────────
    def index_workspace(self, *, force: bool = False) -> dict[str, Any]:
        stats = self.indexer.index_workspace(force=force)
        self._deps = None
        self._emit("code.intel.indexed", stats.to_dict())
        return {"status": "ok", **self.index.status(), "update": stats.to_dict()}

    def index_file(self, rel_path: str) -> dict[str, Any]:
        ok = self.indexer.reindex_file(rel_path)
        self._deps = None
        return {"status": "ok" if ok else "skipped", "path": rel_path}

    def reindex_file(self, rel_path: str) -> dict[str, Any]:
        return self.index_file(rel_path)

    def index_status(self) -> dict[str, Any]:
        return self.index.status()

    # ── language servers ────────────────────────────────────────────────────────
    def start_language_servers(self) -> list[dict[str, Any]]:
        started: list[dict[str, Any]] = []
        for language in self.detect_languages():
            client, reason = self.lsp.ensure_client(self.root, language)
            started.append({
                "language": language,
                "running": client is not None,
                "code": reason or "OK",
            })
        return started

    def stop_language_servers(self) -> int:
        return self.lsp.stop_all()

    def restart_language_server(self, language: str) -> dict[str, Any]:
        return self.lsp.restart(self.root, language)

    def language_server_status(self) -> list[dict[str, Any]]:
        return self.lsp.health_check()

    # ── symbols / search ────────────────────────────────────────────────────────
    def search_symbols(self, query: str, *, kind: Optional[str] = None,
                       language: Optional[str] = None, limit: int = 100) -> dict[str, Any]:
        results = self.index.find_symbols(query, kind=kind, language=language, limit=limit)
        return {"status": "ok", "engine": "index", "query": query, "symbols": results,
                "count": len(results)}

    def find_symbol(self, name: str, *, file: Optional[str] = None, kind: Optional[str] = None,
                    limit: int = 20) -> dict[str, Any]:
        results = self.index.find_symbols(name, kind=kind, file=file, exact=True, limit=limit)
        if not results:
            results = self.index.find_symbols(name, kind=kind, file=file, limit=limit)
        return {"status": "ok", "engine": "index", "query": name, "symbols": results,
                "count": len(results)}

    def get_document_symbols(self, rel_path: str) -> dict[str, Any]:
        language = detect_language(rel_path)
        client = self._lsp_for(language)
        if client is not None:
            try:
                self._ensure_open(client, rel_path)
                raw = client.document_symbols(self.root / rel_path)
                symbols = self._normalize_document_symbols(rel_path, language, raw)
                if symbols:
                    return {"status": "ok", "engine": "lsp", "file": rel_path,
                            "symbols": symbols, "count": len(symbols)}
            except Exception as exc:  # noqa: BLE001 - fall through to the index
                self._emit("code.intel.lsp_fallback", {"op": "documentSymbols", "error": str(exc)[:200]})
        symbols = self.index.find_symbols(file=rel_path, limit=2_000)
        return {"status": "ok", "engine": "index", "file": rel_path,
                "symbols": symbols, "count": len(symbols)}

    def parse_file(self, rel_path: str) -> dict[str, Any]:
        text = self._read(rel_path)
        parsed = self.ast.parse_file(rel_path, text)
        return {"status": "ok", **parsed.to_dict(), "engine": parsed.engine}

    def analyze_file(self, rel_path: str) -> dict[str, Any]:
        text = self._read(rel_path)
        parsed = self.ast.parse_file(rel_path, text)
        diagnostics = self.diagnostics.for_file(rel_path, text)
        imports = self.index.imports_of(rel_path)
        return {
            "status": "ok", "file": rel_path, "language": parsed.language,
            "engine": parsed.engine, "lines": parsed.line_count,
            "symbols": [s.to_dict(compact=True) for s in parsed.symbols],
            "imports": [i.to_dict() for i in parsed.imports],
            "exports": [s.name for s in parsed.exports],
            "routes": [s.name for s in parsed.routes],
            "models": [s.name for s in parsed.models],
            "diagnostics": [d.to_dict() for d in diagnostics],
        }

    # ── definition / references ──────────────────────────────────────────────────
    def get_definition(self, name: str, *, file: Optional[str] = None,
                       line: Optional[int] = None, column: Optional[int] = None) -> dict[str, Any]:
        candidates = self._resolve_symbols(name, file=file)
        if not candidates:
            raise IntelError(NOT_FOUND, f"No symbol named {name!r} was found.")
        target = candidates[0]
        language = target["language"] or detect_language(target["file"])
        client = self._lsp_for(language)
        if client is not None:
            try:
                self._ensure_open(client, target["file"])
                lsp_line, lsp_col = self._symbol_position(target)
                raw = client.definition(self.root / target["file"], lsp_line, lsp_col)
                locations = self._normalize_locations(raw)
                if locations:
                    enriched = [self._enrich_location(loc) for loc in locations]
                    return {"status": "ok", "engine": "lsp", "symbol": name,
                            "definitions": enriched, "count": len(enriched)}
            except Exception as exc:  # noqa: BLE001
                self._emit("code.intel.lsp_fallback", {"op": "definition", "error": str(exc)[:200]})
        declarations = [self._symbol_detail(c) for c in candidates]
        return {"status": "ok", "engine": "index", "symbol": name,
                "definitions": declarations, "count": len(declarations)}

    def find_references(self, name: str, *, file: Optional[str] = None, limit: int = 200) -> dict[str, Any]:
        candidates = self._resolve_symbols(name, file=file)
        if not candidates:
            raise IntelError(NOT_FOUND, f"No symbol named {name!r} was found.")
        target = candidates[0]
        language = target["language"] or detect_language(target["file"])
        client = self._lsp_for(language)
        if client is not None:
            try:
                self._ensure_open(client, target["file"])
                lsp_line, lsp_col = self._symbol_position(target)
                raw = client.references(self.root / target["file"], lsp_line, lsp_col)
                locations = self._normalize_locations(raw)
                if locations:
                    refs = [self._reference_from_location(loc) for loc in locations[:limit]]
                    # Mark the declaration site. A language server's references may or
                    # may not include the declaration; when it does, the hit at the
                    # resolved symbol's own file+line is the definition. Never
                    # fabricate a location — this only flags a real returned hit.
                    decl_file = target["file"]
                    decl_line = target["line"]
                    for r in refs:
                        if r["file"] == decl_file and r["line"] == decl_line:
                            r["definition"] = True
                    return {"status": "ok", "engine": "lsp", "symbol": name,
                            "references": refs, "count": len(refs),
                            "truncated": len(locations) > limit}
            except Exception as exc:  # noqa: BLE001
                self._emit("code.intel.lsp_fallback", {"op": "references", "error": str(exc)[:200]})
        refs = self.index.find_references(name, limit=limit)
        return {"status": "ok", "engine": "index", "symbol": name,
                "references": refs, "count": len(refs), "truncated": len(refs) >= limit}

    def find_references_at_location(self, file: str, line: int, column: int) -> dict[str, Any]:
        symbols = self.index.symbol_at(file, line)
        if symbols:
            return self.find_references(symbols[0]["name"], file=file)
        raise IntelError(NOT_FOUND, "No symbol was found at that location.")

    # ── hover / types ───────────────────────────────────────────────────────────
    def get_hover(self, *, file: str, line: int, column: int) -> dict[str, Any]:
        language = detect_language(file)
        client = self._lsp_for(language)
        if client is not None:
            try:
                self._ensure_open(client, file)
                raw = client.hover(self.root / file, line, column)
                contents = (raw or {}).get("contents")
                text = _hover_text(contents)
                if text:
                    return {"status": "ok", "engine": "lsp", "file": file, "line": line,
                            "column": column, "documentation": text}
            except Exception as exc:  # noqa: BLE001
                self._emit("code.intel.lsp_fallback", {"op": "hover", "error": str(exc)[:200]})
        symbols = self.index.symbol_at(file, line)
        if symbols:
            symbol = symbols[0]
            return {"status": "ok", "engine": "index", "file": file, "line": line,
                    "column": column, "symbol": symbol["name"], "kind": symbol["kind"],
                    "signature": symbol["signature"], "documentation": ""}
        return {"status": "ok", "engine": "index", "file": file, "line": line,
                "column": column, "documentation": "", "note": "no symbol at that location"}

    def get_type_information(self, name: str, *, file: Optional[str] = None) -> dict[str, Any]:
        candidates = self._resolve_symbols(name, file=file)
        if not candidates:
            raise IntelError(NOT_FOUND, f"No symbol named {name!r} was found.")
        target = candidates[0]
        hover = self.get_hover(file=target["file"], line=target["line"], column=target.get("column", 1))
        return {
            "status": "ok", "engine": hover.get("engine"), "symbol": name,
            "kind": target["kind"], "signature": target.get("signature", ""),
            "type": target.get("type", ""), "documentation": hover.get("documentation", ""),
            "location": {"file": target["file"], "line": target["line"], "column": target.get("column", 1)},
        }

    # ── implementations ───────────────────────────────────────────────────────
    def get_implementations(self, name: str, *, file: Optional[str] = None) -> dict[str, Any]:
        candidates = self._resolve_symbols(name, file=file)
        if not candidates:
            raise IntelError(NOT_FOUND, f"No symbol named {name!r} was found.")
        target = candidates[0]
        language = target["language"] or detect_language(target["file"])
        client = self._lsp_for(language)
        lsp_impls: list[dict[str, Any]] = []
        engine = "index"
        if client is not None:
            try:
                self._ensure_open(client, target["file"])
                lsp_line, lsp_col = self._symbol_position(target)
                raw = client.implementations(self.root / target["file"], lsp_line, lsp_col)
                locations = self._normalize_locations(raw)
                if locations:
                    lsp_impls = [self._enrich_location(loc) for loc in locations]
                    engine = "lsp"
            except Exception as exc:  # noqa: BLE001
                self._emit("code.intel.lsp_fallback", {"op": "implementations", "error": str(exc)[:200]})
        # Merge the structural heritage result (a class line `extends X implements Y`)
        # with the semantic result: a language server may under-report when a module
        # fails to resolve, and the index may miss a dynamic case — the union is the
        # honest answer, and each entry keeps its file/line.
        heritage = self.index.implementations_of(name)
        by_key: dict[tuple[str, int], dict[str, Any]] = {}
        for item in lsp_impls:
            key = (item.get("file", ""), int(item.get("line", 0) or 0))
            by_key[key] = {**item, "engine": "lsp"}
        for item in heritage:
            key = (item.get("file", ""), int(item.get("line", 0) or 0))
            if key not in by_key:
                by_key[key] = item
        impls = list(by_key.values())
        engines = sorted({i.get("engine", "index") for i in impls}) or [engine]
        return {"status": "ok", "engine": "+".join(engines), "symbol": name,
                "implementations": impls, "count": len(impls),
                "note": "union of semantic (language server) and structural (index) results"}

    # ── imports / exports / dependencies ──────────────────────────────────────────
    def get_imports(self, rel_path: str) -> dict[str, Any]:
        imports = self.index.imports_of(rel_path)
        return {"status": "ok", "engine": "index", "file": rel_path,
                "imports": imports, "count": len(imports)}

    def get_exports(self, rel_path: str) -> dict[str, Any]:
        exports = [s for s in self.index.find_symbols(file=rel_path, limit=2_000) if s.get("exported")]
        return {"status": "ok", "engine": "index", "file": rel_path,
                "exports": exports, "count": len(exports)}

    def get_importers(self, rel_path: str) -> dict[str, Any]:
        return {"status": "ok", "engine": "index", "file": rel_path,
                "importers": self.index.importers_of(rel_path)}

    def get_dependencies(self, *, file: Optional[str] = None, symbol: Optional[str] = None) -> dict[str, Any]:
        graph = self._dependency_graph()
        if symbol:
            candidates = self._resolve_symbols(symbol)
            if not candidates:
                raise IntelError(NOT_FOUND, f"No symbol named {symbol!r} was found.")
            deps = graph.dependencies_for_symbol(candidates[0]["id"])
            return {"status": "ok", "engine": "index", "symbol": symbol,
                    "dependencies": deps, "count": len(deps)}
        if not file:
            raise IntelError(NOT_FOUND, "A file or symbol is required.")
        return {"status": "ok", "engine": "index", "file": file,
                "dependencies": graph.get_dependencies(file),
                "count": len(graph.get_dependencies(file)),
                "transitive": graph.transitive_dependencies(file)}

    def get_dependents(self, *, file: Optional[str] = None, symbol: Optional[str] = None) -> dict[str, Any]:
        graph = self._dependency_graph()
        if symbol:
            candidates = self._resolve_symbols(symbol)
            if not candidates:
                raise IntelError(NOT_FOUND, f"No symbol named {symbol!r} was found.")
            return {"status": "ok", "engine": "index", "symbol": symbol,
                    "dependents": graph.dependents_for_symbol(candidates[0]["id"]),
                    "count": len(graph.dependents_for_symbol(candidates[0]["id"]))}
        if not file:
            raise IntelError(NOT_FOUND, "A file or symbol is required.")
        return {"status": "ok", "engine": "index", "file": file,
                "dependents": graph.get_dependents(file),
                "count": len(graph.get_dependents(file))}

    def get_dependency_graph(self, *, max_nodes: int = 500) -> dict[str, Any]:
        graph = self._dependency_graph()
        result = graph.to_dict(max_nodes=max_nodes)
        return {"status": "ok", "engine": "index", **result}

    # ── call hierarchy ──────────────────────────────────────────────────────────
    def get_call_hierarchy(self, name: str, *, file: Optional[str] = None, direction: str = "both") -> dict[str, Any]:
        candidates = self._resolve_symbols(name, file=file)
        if not candidates:
            raise IntelError(NOT_FOUND, f"No symbol named {name!r} was found.")
        target = candidates[0]
        incoming = self.get_incoming_calls(name, file=file) if direction in ("in", "both", "incoming") else None
        outgoing = self.get_outgoing_calls(name, file=file) if direction in ("out", "both", "outgoing") else None
        return {
            "status": "ok", "engine": target.get("engine", "index"), "symbol": name,
            "incoming": (incoming or {}).get("calls", []),
            "outgoing": (outgoing or {}).get("calls", []),
            "incomingEngine": (incoming or {}).get("engine"),
            "outgoingEngine": (outgoing or {}).get("engine"),
        }

    def get_incoming_calls(self, name: str, *, file: Optional[str] = None) -> dict[str, Any]:
        candidates = self._resolve_symbols(name, file=file)
        if not candidates:
            raise IntelError(NOT_FOUND, f"No symbol named {name!r} was found.")
        target = candidates[0]
        language = target["language"] or detect_language(target["file"])
        client = self._lsp_for(language)
        if client is not None:
            try:
                self._ensure_open(client, target["file"])
                lsp_line, lsp_col = self._symbol_position(target)
                items = client.prepare_call_hierarchy(self.root / target["file"], lsp_line, lsp_col)
                if items:
                    raw = client.incoming_calls(items[0])
                    calls = self._normalize_incoming(raw)
                    if calls:
                        return {"status": "ok", "engine": "lsp", "symbol": name, "calls": calls, "count": len(calls)}
            except Exception as exc:  # noqa: BLE001
                self._emit("code.intel.lsp_fallback", {"op": "incomingCalls", "error": str(exc)[:200]})
        edges = self.index.callers_of(name)
        calls = [{"from": e["caller_name"] or e["caller"], "file": e["rel"], "line": e["line"]} for e in edges]
        return {"status": "ok", "engine": "index", "symbol": name, "calls": calls, "count": len(calls)}

    def get_outgoing_calls(self, name: str, *, file: Optional[str] = None) -> dict[str, Any]:
        candidates = self._resolve_symbols(name, file=file)
        if not candidates:
            raise IntelError(NOT_FOUND, f"No symbol named {name!r} was found.")
        target = candidates[0]
        language = target["language"] or detect_language(target["file"])
        client = self._lsp_for(language)
        if client is not None:
            try:
                self._ensure_open(client, target["file"])
                lsp_line, lsp_col = self._symbol_position(target)
                items = client.prepare_call_hierarchy(self.root / target["file"], lsp_line, lsp_col)
                if items:
                    raw = client.outgoing_calls(items[0])
                    calls = self._normalize_outgoing(raw)
                    if calls:
                        return {"status": "ok", "engine": "lsp", "symbol": name, "calls": calls, "count": len(calls)}
            except Exception as exc:  # noqa: BLE001
                self._emit("code.intel.lsp_fallback", {"op": "outgoingCalls", "error": str(exc)[:200]})
        edges = self.index.callees_of(name)
        calls = [{"to": e["callee_name"] or e["callee"], "file": e["rel"], "line": e["line"]} for e in edges]
        return {"status": "ok", "engine": "index", "symbol": name, "calls": calls, "count": len(calls)}

    # ── diagnostics ─────────────────────────────────────────────────────────────
    def get_diagnostics(self, *, file: Optional[str] = None, severity: Optional[str] = None) -> dict[str, Any]:
        from .diagnostics import summarize

        diagnostics: list[CodeDiagnostic] = []
        engine = "index"
        if file:
            text = self._read(file)
            lsp_raw = None
            client = self._lsp_for(detect_language(file))
            if client is not None:
                try:
                    self._ensure_open(client, file)
                    lsp_raw = client.published_diagnostics(self.root / file)
                    if not lsp_raw:
                        lsp_raw = client.wait_for_diagnostics(self.root / file, timeout_s=3.0)
                    engine = "lsp" if lsp_raw else engine
                except Exception:  # noqa: BLE001
                    lsp_raw = None
            diagnostics = self.diagnostics.for_file(file, text, lsp=lsp_raw)
            self.diagnostics.record(file, diagnostics)
        else:
            for row in self.index.file_rows(limit=10_000):
                diagnostics.extend(self.diagnostics.cached(row["rel"]))
        if severity:
            diagnostics = [d for d in diagnostics if d.severity == severity]
        payload = [d.to_dict() for d in diagnostics]
        return {"status": "ok", "engine": engine, "file": file,
                "diagnostics": payload, "count": len(payload), "summary": summarize(diagnostics)}

    # ── semantic editing ─────────────────────────────────────────────────────────
    def rename_symbol(self, name: str, new_name: str, *, file: Optional[str] = None,
                      dry_run: bool = True) -> RenameResult:
        if not new_name or not new_name.strip():
            raise IntelError(EDIT_CONFLICT, "A new name is required.")
        candidates = self._resolve_symbols(name, file=file)
        if not candidates:
            raise IntelError(NOT_FOUND, f"No symbol named {name!r} was found.")
        target = candidates[0]
        language = target["language"] or detect_language(target["file"])
        client = self._lsp_for(language)
        if client is not None:
            try:
                self._ensure_open(client, target["file"])
                lsp_line, lsp_col = self._symbol_position(target)
                raw = client.rename(self.root / target["file"], lsp_line, lsp_col, new_name)
                files = parse_workspace_edit(raw or {})
                if files:
                    return RenameResult(
                        symbol=name, new_name=new_name, files=files, engine="lsp",
                        applied=False, note="semantic rename from the language server",
                    )
            except Exception as exc:  # noqa: BLE001
                self._emit("code.intel.lsp_fallback", {"op": "rename", "error": str(exc)[:200]})
        return self.editor.rename_symbol_fallback(name, new_name, file=file or "", dry_run=dry_run)

    def apply_workspace_edit(self, files: list[FileEdit], *, write_root: Optional[Path] = None,
                             dry_run: bool = False) -> dict[str, Any]:
        target_root = Path(write_root) if write_root else self.root
        rebased = rebase_edits(self.root, target_root, files)
        return apply_file_edits(target_root, rebased, dry_run=dry_run)

    def organize_imports(self, rel_path: Optional[str] = None) -> dict[str, Any]:
        return organize_imports_fallback(self.index)

    def get_code_actions(self, *, file: str, line: int, column: int, kind: Optional[str] = None) -> dict[str, Any]:
        client = self._lsp_for(detect_language(file))
        if client is None:
            return {"status": "ok", "engine": "index", "actions": [], "count": 0,
                    "note": "code actions need a language server"}
        try:
            self._ensure_open(client, file)
            node = {
                "start": {"line": line - 1, "character": column - 1},
                "end": {"line": line - 1, "character": column},
            }
            raw = client.code_action(self.root / file, node, kind=kind)
            actions = [a for a in (raw or []) if isinstance(a, dict)]
            return {"status": "ok", "engine": "lsp", "actions": [
                {"title": a.get("title", ""), "kind": a.get("kind", "")} for a in actions
            ], "count": len(actions)}
        except Exception as exc:  # noqa: BLE001
            return {"status": "ok", "engine": "lsp", "actions": [], "count": 0,
                    "error": str(exc)[:200]}

    # ── context engine ───────────────────────────────────────────────────────────
    def get_relevant_context(self, query: str, *, max_files: int = 12, max_symbols: int = 40,
                             budget_s: Optional[float] = None) -> dict[str, Any]:
        engine = CodeContextEngine(self)
        return engine.select(query, max_files=max_files, max_symbols=max_symbols, budget_s=budget_s)

    # ── git ─────────────────────────────────────────────────────────────────────
    def changed_files(self) -> dict[str, Any]:
        result = fs_git.status(self.root)
        return {"status": "ok", **{k: result.get(k) for k in ("available", "entries", "count", "reason")}}

    def changed_symbols(self) -> dict[str, Any]:
        """Symbols in files that git reports changed (combines git + the index)."""
        status = fs_git.status(self.root)
        if not status.get("available"):
            return {"status": "ok", "available": False, "symbols": [], "count": 0,
                    "reason": status.get("reason")}
        out: list[dict[str, Any]] = []
        for entry in status.get("entries", []):
            if entry["state"] == "deleted":
                continue
            for symbol in self.index.find_symbols(file=entry["path"], limit=200):
                out.append({**symbol, "gitState": entry["state"]})
        return {"status": "ok", "available": True, "symbols": out, "count": len(out)}

    def symbol_history(self, name: str) -> dict[str, Any]:
        """Whether a symbol exists today and where. A git-blame read is deferred
        to the existing git subsystem; this reports the current declarations."""
        candidates = self._resolve_symbols(name)
        return {"status": "ok", "symbol": name,
                "declarations": [self._symbol_detail(c) for c in candidates],
                "count": len(candidates),
                "note": "history/blame requires the git subsystem; unavailable here"}

    # ── test intelligence ────────────────────────────────────────────────────────
    def find_related_tests(self, *, symbol: Optional[str] = None, file: Optional[str] = None) -> dict[str, Any]:
        from .context import find_related_tests

        return find_related_tests(self, symbol=symbol, file=file)

    # ── helpers ─────────────────────────────────────────────────────────────────
    def _dependency_graph(self) -> DependencyGraph:
        if self._deps is None:
            self._deps = DependencyGraph(self.index)
        return self._deps

    def _lsp_for(self, language: str):
        client, _reason = self.lsp.ensure_client(self.root, language)
        return client

    def sync_document(self, file: str) -> bool:
        """Push the file's CURRENT text to the language server, if one is live.

        An edit happens on disk, outside the LSP client. Without this the server
        would report diagnostics for the pre-edit text. Returns False when no
        client serves the language; never raises.
        """
        try:
            text = self._read(file)
            client = self._lsp_for(detect_language(file))
            if client is None:
                return False
            path = self.root / file
            if client.is_open(path):
                client.change_document(path, text)
            else:
                client.open_document(path, text)
                self._open_docs.add(file)
            return True
        except Exception:  # noqa: BLE001 - a sync failure is not fatal
            return False

    def _ensure_open(self, client: Any, rel_path: str) -> None:
        from .lsp.client import LSPClient  # noqa: PLC0415

        if not isinstance(client, LSPClient):  # pragma: no cover - defensive
            return
        path = self.root / rel_path
        if client.is_open(path):
            return
        try:
            text = fs_encoding.read_text(path).text
        except Exception:  # noqa: BLE001
            return
        client.open_document(path, text)
        self._open_docs.add(rel_path)

    def _read(self, rel_path: str) -> str:
        try:
            return fs_encoding.read_text(self.root / rel_path).text
        except Exception as exc:  # noqa: BLE001
            raise IntelError(NOT_FOUND, f"Could not read {rel_path!r}: {exc}") from exc

    def _symbol_position(self, symbol: dict[str, Any]) -> tuple[int, int]:
        """The 1-based (line, column) where the symbol's NAME begins.

        The extractor records a class/method at its declaration line but the column
        can be 1 (indentation). A language server resolves nothing at whitespace, so
        for an LSP call we locate the real name column from the source; the declared
        column is the fallback.
        """
        line = int(symbol.get("line", 1) or 1)
        name = str(symbol.get("name", ""))
        column = int(symbol.get("column", 1) or 1)
        if not name or line < 1:
            return line, column
        try:
            text_lines = fs_encoding.read_text(self.root / symbol["file"]).text.splitlines()
            if 1 <= line <= len(text_lines):
                idx = text_lines[line - 1].find(name)
                if idx >= 0:
                    return line, idx + 1
        except Exception:  # noqa: BLE001 - a read failure keeps the declared column
            pass
        return line, column

    def _resolve_symbols(self, name: str, *, file: Optional[str] = None) -> list[dict[str, Any]]:
        # When a file is given, scope the lookup to it FIRST — the same name exists in
        # many modules (`getUser`, `process`), so file-scoping is the disambiguator.
        if file:
            scoped = self.index.find_symbols(name, file=file, limit=50)
            scoped = [s for s in scoped if s["kind"] != "import"] or scoped
            if scoped:
                return scoped
        results = self.index.find_symbols(name, exact=True, limit=50)
        # A name can match an `import` alias and a real declaration in different
        # files. Prefer a real declaration (anything that is not an `import`) so a
        # definition/rename/implementation query resolves the symbol, not the import.
        if results:
            declarations = [s for s in results if s["kind"] != "import"]
            if declarations:
                results = declarations + [s for s in results if s["kind"] == "import"]
        if results:
            return results
        return self.index.find_symbols(name, limit=50)

    def _symbol_detail(self, symbol: dict[str, Any]) -> dict[str, Any]:
        return {
            "symbol": symbol["name"], "kind": symbol["kind"],
            "location": {"file": symbol["file"], "line": symbol["line"], "column": symbol.get("column", 1)},
            "signature": symbol.get("signature", ""),
            "language": symbol.get("language", ""),
            "context": self._line_context(symbol["file"], symbol["line"]),
        }

    def _enrich_location(self, loc: dict[str, Any]) -> dict[str, Any]:
        rel = self._rel(loc["file"])
        symbols = self.index.symbol_at(rel, loc["line"]) if rel else []
        name = symbols[0]["name"] if symbols else ""
        return {
            "file": rel, "line": loc["line"], "column": loc.get("column", 1),
            "symbol": name,
            "signature": symbols[0].get("signature", "") if symbols else "",
            "context": self._line_context(rel, loc["line"]),
            # A stable `location` object so the LSP and index paths return ONE shape
            # (an agent must not have to branch on which engine answered).
            "location": {"file": rel, "line": loc["line"], "column": loc.get("column", 1)},
        }

    def _reference_from_location(self, loc: dict[str, Any]) -> dict[str, Any]:
        rel = self._rel(loc["file"])
        return {"file": rel, "line": loc["line"], "column": loc.get("column", 1),
                "text": self._line_context(rel, loc["line"]), "engine": "lsp"}

    def _line_context(self, rel: str, line: int, *, window: int = 0) -> str:
        try:
            lines = fs_encoding.read_text(self.root / rel).text.splitlines()
        except Exception:  # noqa: BLE001
            return ""
        index = line - 1
        if index < 0 or index >= len(lines):
            return ""
        return lines[index].strip()[:200]

    def _rel(self, path: str) -> str:
        try:
            return Path(path).resolve().relative_to(self.root).as_posix()
        except (ValueError, OSError):
            return path

    def _normalize_locations(self, raw: Any) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        if raw is None:
            return out
        items = raw if isinstance(raw, list) else ([raw] if isinstance(raw, dict) else [])
        for item in items:
            if not isinstance(item, dict):
                continue
            # Three LSP result shapes: Location (`uri`+`range`), LocationLink
            # (`targetUri`+`targetSelectionRange`/`targetRange`), and the definition
            # `targetUri`/`targetRange` form. Handle all of them.
            uri = item.get("uri") or item.get("targetUri")
            range_ = (
                item.get("range")
                or item.get("targetSelectionRange")
                or item.get("targetRange")
                or {}
            )
            if not uri:
                continue
            start = (range_ or {}).get("start") or {}
            out.append({
                "file": _uri_to_path(uri),
                "line": int(start.get("line", 0)) + 1,
                "column": int(start.get("character", 0)) + 1,
            })
        return out

    def _normalize_document_symbols(self, rel: str, language: str, raw: Any) -> list[dict[str, Any]]:
        from .lsp.client import range_from_lsp

        out: list[dict[str, Any]] = []

        def _walk(node: Any, container: str = "") -> None:
            if not isinstance(node, dict):
                return
            range_ = node.get("range") or node.get("location", {}).get("range") or {}
            pos = range_from_lsp(range_) if range_ else {}
            name = node.get("name", "")
            kind = _lsp_symbol_kind(node.get("kind"))
            if name:
                out.append({
                    "id": f"{self.project_id}:{rel}:{pos.get('line', 0)}:1:{name}",
                    "name": name, "kind": kind, "file": rel,
                    "line": pos.get("line", 0), "column": 1,
                    "endLine": pos.get("endLine", 0), "endColumn": pos.get("endColumn", 0),
                    "language": language, "container": container, "engine": "lsp",
                    "exported": False, "signature": "",
                })
            for child in node.get("children") or []:
                _walk(child, name or container)

        items = raw if isinstance(raw, list) else []
        for item in items:
            _walk(item)
        return out

    def _normalize_incoming(self, raw: Any) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for item in raw or []:
            if not isinstance(item, dict):
                continue
            from_item = item.get("from") or {}
            pos = range_from_lsp(from_item.get("selectionRange") or from_item.get("range") or {})
            file = _uri_to_path(from_item.get("uri", ""))
            out.append({"from": from_item.get("name", ""), "file": self._rel(file),
                        "line": pos.get("line", 0)})
        return out

    def _normalize_outgoing(self, raw: Any) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for item in raw or []:
            if not isinstance(item, dict):
                continue
            to_item = item.get("to") or {}
            pos = range_from_lsp(to_item.get("selectionRange") or to_item.get("range") or {})
            file = _uri_to_path(to_item.get("uri", ""))
            out.append({"to": to_item.get("name", ""), "file": self._rel(file),
                        "line": pos.get("line", 0)})
        return out

    def _emit(self, event_type: str, data: dict[str, Any]) -> None:
        if self._on_event is None:
            return
        try:
            self._on_event(event_type, data)
        except Exception:  # noqa: BLE001
            pass


def rebase_edits(source_root: Path, target_root: Path, files: list[FileEdit]) -> list[FileEdit]:
    """Rebase LSP edits (rooted at the analysis root) onto a write root.

    A rename computed against the read-only checkout is applied to the disposable
    copy (or the chosen folder) by rewriting each file path relative to the
    analysis root and re-anchoring it under the write root. A file that is not under
    the analysis root is dropped (it can only be a library/virtual doc).
    """
    source = Path(source_root).resolve()
    target = Path(target_root).resolve()
    if source == target:
        return files
    out: list[FileEdit] = []
    for file_edit in files:
        raw = Path(file_edit.file)
        candidate = raw if raw.is_absolute() else (source / raw)
        try:
            rel = candidate.resolve().relative_to(source).as_posix()
        except (ValueError, OSError):
            continue
        out.append(FileEdit(file=str(target / rel), edits=file_edit.edits, engine=file_edit.engine))
    return out


def _hover_text(contents: Any) -> str:
    if contents is None:
        return ""
    if isinstance(contents, str):
        return contents[:2_000]
    if isinstance(contents, dict):
        return str(contents.get("value", ""))[:2_000]
    if isinstance(contents, list):
        parts = []
        for item in contents:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                parts.append(str(item.get("value", "")))
        return "\n".join(parts)[:2_000]
    return str(contents)[:2_000]


_LSP_SYMBOL_KINDS = {
    1: "file", 2: "module", 3: "namespace", 4: "package", 5: "class", 6: "method",
    7: "property", 8: "field", 9: "constructor", 10: "enum", 11: "interface",
    12: "function", 13: "variable", 14: "constant", 15: "string", 16: "number",
    17: "boolean", 18: "array", 19: "object", 20: "key", 21: "null", 22: "enum",
    23: "struct", 24: "event", 25: "operator", 26: "typeParameter",
}


def _lsp_symbol_kind(kind: Any) -> str:
    try:
        return _LSP_SYMBOL_KINDS.get(int(kind), "unknown")
    except (TypeError, ValueError):
        return "unknown"


def _uri_to_path(uri: str) -> str:
    from .lsp.client import uri_to_path

    return uri_to_path(uri)


__all__ = ["CodeIntelligenceManager", "rebase_edits"]
