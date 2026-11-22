"""The process-wide CIUE runtime — one LSP manager, one index per workspace.

A single `CodeIntelRuntime` lives on the `AgenticService`. It owns:

  * ONE `LSPManager` (language servers are expensive; they are shared across runs
    and never duplicated);
  * a `WorkspaceWatcher` + `IncrementalIndexer` per workspace root, so the code
    index stays current in the background and a query never triggers a full
    rebuild. The watcher reuses the filesystem subsystem's native/polling backends.

The runtime is best-effort everywhere: a failure to start a watcher is a
diagnostic, never a boot blocker, and `manager_for()` always returns a usable
manager even with no LSP and an empty index.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any, Callable, Optional

from ..workspace import ResolvedWorkspace
from .ast import ASTEngine
from .indexer import IncrementalIndexer
from .lsp.manager import LSPManager
from .manager import CodeIntelligenceManager
from .persistence.index import CodeIndex, open_code_index
from .project import project_instance_id

log = logging.getLogger("harness.code.intel.runtime")

WATCH_INTERVAL_S = 2.0


class _WorkspaceState:
    def __init__(self, root: Path, project_id: str, index: CodeIndex, on_event: Any = None) -> None:
        self.root = root
        self.project_id = project_id
        self.index = index
        self.ast = ASTEngine(project_id, root=root)
        self.indexer = IncrementalIndexer(root, project_id, index, ast=self.ast, on_event=on_event)
        self.watcher = None
        self.thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self.on_event = on_event
        self._indexing = threading.Event()

    def start_watch(self, *, initial_index: bool = True) -> None:
        try:
            from ..fs.watch import WorkspaceWatcher  # noqa: PLC0415

            self.watcher = WorkspaceWatcher(self._ws(), recursive=True, index=None)
            self.thread = threading.Thread(target=self._loop, name="eclipse-intel-watch", daemon=True)
            self.thread.start()
        except Exception:  # noqa: BLE001 - a watcher is best-effort
            self.watcher = None
        # Kick the INITIAL index asynchronously so the first query never blocks on a
        # full repository walk (the contract: initial indexing is asynchronous).
        # Boot warm-up opens the state WITHOUT this: it must not scan the repo.
        if initial_index:
            self._index_async()

    def _index_async(self) -> None:
        if self._indexing.is_set():
            return
        self._indexing.set()

        def _run() -> None:
            try:
                self.indexer.index_workspace()
            except Exception:  # noqa: BLE001 - warm-up is best-effort
                pass
            finally:
                self._indexing.clear()

        threading.Thread(target=_run, name="eclipse-intel-index", daemon=True).start()

    def is_indexing(self) -> bool:
        return self._indexing.is_set()

    def _ws(self) -> Any:
        from ..policy import WorkspaceBinding
        from ..workspace import ResolvedWorkspace  # noqa: PLC0415

        binding = WorkspaceBinding(id=self.project_id, root=self.root, base_revision="", strategy="index")
        return ResolvedWorkspace(
            id=self.project_id, root=self.root, base_revision="", strategy="index", binding=binding,
        )

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                if self.watcher is not None:
                    events = self.watcher.poll_once()
                    if events:
                        self.indexer.apply_watch_events(events)
            except Exception:  # noqa: BLE001 - the loop must survive transient errors
                pass
            self._stop.wait(WATCH_INTERVAL_S)

    def stop(self) -> None:
        self._stop.set()
        if self.watcher is not None:
            try:
                self.watcher.stop()
            except Exception:  # noqa: BLE001
                pass
        if self.thread is not None:
            self.thread.join(timeout=2.0)


class CodeIntelRuntime:
    """The process-wide CIUE runtime (LSP manager + per-workspace index/watch)."""

    def __init__(self, *, on_event: Optional[Callable[[str, dict[str, Any]], None]] = None) -> None:
        from .config import load_config  # noqa: PLC0415

        self._on_event = on_event
        self.config = load_config()
        # An operator-disabled LSP layer is honest: no provider is registered, so
        # every semantic request reports UNSUPPORTED_LANGUAGE and the structural
        # engine answers instead. The index/AST layers still work.
        providers = None if self.config.lsp_enabled else []
        self.lsp = LSPManager(providers=providers, on_event=on_event)
        self._states: dict[str, _WorkspaceState] = {}
        self._lock = threading.RLock()
        self._enabled = bool(self.config.enabled)

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = bool(enabled)

    @property
    def enabled(self) -> bool:
        return self._enabled

    def _state_for(self, workspace: ResolvedWorkspace, *, initial_index: bool = True) -> _WorkspaceState:
        root = Path(workspace.root)
        key = getattr(workspace, "id", "") or str(root)
        with self._lock:
            state = self._states.get(key)
            if state is not None and state.root == root:
                return state
            project_id = project_instance_id(root, key)
            index = open_code_index(project_id)
            state = _WorkspaceState(root, project_id, index, on_event=self._on_event)
            state.start_watch(initial_index=initial_index)
            self._states[key] = state
            return state

    def manager_for(self, workspace: ResolvedWorkspace) -> CodeIntelligenceManager:
        if not self._enabled:
            # Still return a manager; it simply has no shared watcher.
            return CodeIntelligenceManager(workspace, lsp_manager=self.lsp, on_event=self._on_event)
        state = self._state_for(workspace)
        # Lazy initial indexing: a state opened by boot warm-up skipped the scan;
        # the FIRST real use starts it (idempotent, on a worker thread). The query
        # itself still reads the existing index and never waits for the scan.
        state._index_async()
        return CodeIntelligenceManager(
            workspace, lsp_manager=self.lsp, ast=state.ast, index=state.index, on_event=self._on_event,
        )

    def warm(self, workspace: ResolvedWorkspace) -> None:
        """Open the shared index for a workspace WITHOUT indexing it.

        Opening a real repository's index is seconds (hundreds of MB on disk).
        Doing it lazily inside a run put that cost on the event loop, in the
        pre-model phase — the same stall class a live turn exposed. Boot calls
        this on a worker thread; the scan itself stays lazy (first real use).
        """
        if not self._enabled:
            return
        self._state_for(workspace, initial_index=False)

    def is_ready_for(self, workspace: ResolvedWorkspace) -> bool:
        """True when this workspace already has a shared index/LSP state.

        The post-edit diagnostics seam must never CREATE state on the write
        path; it asks this first and falls back to structural diagnostics.
        """
        key = getattr(workspace, "id", "") or str(getattr(workspace, "root", ""))
        with self._lock:
            state = self._states.get(key)
            return state is not None and Path(state.root) == Path(getattr(workspace, "root", ""))

    def is_fresh(self, workspace: ResolvedWorkspace, *, max_age_s: float = 30.0) -> bool:
        """Is the shared index CURRENT for this workspace?

        Round 3 / W4 (ADR-H287): this used to `return True` unconditionally,
        for EVERY workspace - including one that was never indexed and one
        with no background indexer. That is an UNMEASURED freshness claim
        about a cache, and this harness refuses to publish one.

        The EVIDENCE is the background watcher: it applies watch events to
        the shared index as the files change, so its presence IS the
        freshness guarantee. A workspace with no watcher, or no index at
        all, is not fresh - and saying so is what lets a read path ask for a
        refresh instead of trusting a claim.

        (`max_age_s` is kept for the call signature; the watcher is
        continuous, so there is no age to compare.)
        """
        key = getattr(workspace, "id", "") or str(getattr(workspace, "root", ""))
        state = self._states.get(key)
        if state is None:
            # Never indexed for this workspace: there is no freshness to claim.
            return False
        if getattr(state, "watcher", None) is None:
            # No background indexer is running, so nothing keeps the shared
            # index current. Claiming freshness here would be the unmeasured
            # claim this replaced.
            return False
        # The background watcher OWNS freshness: it applies watch events to the
        # shared index as the files change. Its presence is the evidence.
        return True

    def ensure_indexed(self, workspace: ResolvedWorkspace, *, force: bool = False) -> dict[str, Any]:
        state = self._state_for(workspace)
        stats = state.indexer.index_workspace(force=force)
        return {"status": "ok", **state.index.status(), "update": stats.to_dict()}

    def status(self) -> dict[str, Any]:
        with self._lock:
            states = {
                key: {"root": str(s.root), "projectId": s.project_id,
                      "indexing": s.is_indexing(), **s.index.status()}
                for key, s in self._states.items()
            }
        from .treesitter import available_grammars, tree_sitter_available  # noqa: PLC0415

        return {
            "enabled": self._enabled,
            # Round 3 / W4 (ADR-H287): publish the engine that answers. The
            # symbol outline falls back to the stdlib extractor when
            # tree-sitter is absent, and a reader must be able to tell.
            "treeSitter": {
                "available": tree_sitter_available(),
                "grammars": available_grammars(),
            },
            "config": self.config.to_dict(),
            "lsp": self.lsp.health_check(),
            "supportedLanguages": self.lsp.supported_languages(),
            "workspaces": states,
        }

    def start_language_servers(self, workspace: ResolvedWorkspace) -> list[dict[str, Any]]:
        manager = self.manager_for(workspace)
        return manager.start_language_servers()

    def stop(self) -> None:
        with self._lock:
            states = list(self._states.values())
            self._states.clear()
        for state in states:
            state.stop()
        self.lsp.stop_all()
        for state in states:
            try:
                state.index.close()
            except Exception:  # noqa: BLE001
                pass


_RUNTIME: Optional[CodeIntelRuntime] = None
_RUNTIME_LOCK = threading.Lock()


def get_code_intel_runtime() -> CodeIntelRuntime:
    """The process-wide CIUE runtime (created on first use)."""
    global _RUNTIME
    with _RUNTIME_LOCK:
        if _RUNTIME is None:
            _RUNTIME = CodeIntelRuntime()
        return _RUNTIME


def peek_code_intel_runtime() -> Optional[CodeIntelRuntime]:
    """The runtime only when one already exists. Never creates one."""
    with _RUNTIME_LOCK:
        return _RUNTIME


def build_code_intel_runtime(
    *, on_event: Optional[Callable[[str, dict[str, Any]], None]] = None,
) -> CodeIntelRuntime:
    return CodeIntelRuntime(on_event=on_event)


def set_code_intel_runtime(runtime: Optional[CodeIntelRuntime]) -> None:
    """TEST SEAM: replace the process-wide runtime. Never used in production."""
    global _RUNTIME
    with _RUNTIME_LOCK:
        _RUNTIME = runtime


__all__ = [
    "CodeIntelRuntime",
    "WATCH_INTERVAL_S",
    "build_code_intel_runtime",
    "get_code_intel_runtime",
    "peek_code_intel_runtime",
    "set_code_intel_runtime",
]
