"""FileSystemService — the single, deterministic facade over the fs subsystem.

Every filesystem tool handler calls this object; it calls the operation modules.
It is NOT coupled to the LLM: a CLI, a UI, a background worker, a future
sub-agent or an MCP server can call the same methods. The LLM decides WHAT to
change; this service determines HOW to perform it safely.

All authorization flows through `permissions.require` via `FsContext`; all path
resolution flows through `resolver`; all mutations are atomic and reversible.
"""

from __future__ import annotations

from typing import Any, Optional

from ..workspace import ResolvedWorkspace
from . import editing, git as git_mod, index as index_mod, metadata, operations, search, symbols, verify, watch
from .context import FsContext
from .errors import WORKSPACE_UNBOUND, FsError
from .resolver import workspace_info

# Read-only operations that are always allowed for an allowlisted run.
READ_CAPABILITY = "read"

# Watcher kinds → canonical Event System names. The watcher already debounces and
# coalesces, so this is not an event storm.
_FILE_EVENT_BY_KIND = {
    "created": "file.created",
    "modified": "file.changed",
    "deleted": "file.deleted",
    "renamed": "file.changed",
}


def _publish_watch_events(events: list[Any]) -> None:
    """Fan debounced watcher events onto the canonical Event System (best-effort)."""
    from ...event_system.emit import publish_event  # noqa: PLC0415

    for event in events:
        canonical = _FILE_EVENT_BY_KIND.get(getattr(event, "event", ""))
        if canonical:
            publish_event(canonical, {
                "path": getattr(event, "path", None),
                "oldPath": getattr(event, "old_path", None),
            }, source="code.fs")


class FileSystemService:
    """One workspace-bound filesystem service instance (per run)."""

    def __init__(
        self,
        ws: Optional[ResolvedWorkspace],
        binding: Any,
        actor: Any,
        *,
        config: Optional[dict[str, Any]] = None,
        index: Any = None,
        watcher: Any = None,
        run_id: Optional[str] = None,
        turn_key: Optional[str] = None,
    ) -> None:
        self._ws = ws
        self._binding = binding
        self._actor = actor
        self._config = config or {}
        self._index = index
        self._watcher = watcher
        self._run_id = str(run_id or "")
        self._turn_key = str(turn_key or run_id or "")

    @property
    def workspace(self) -> Optional[ResolvedWorkspace]:
        """The bound workspace, so a caller can name an ABSOLUTE path it wrote to."""
        return self._ws

    def _before_mutation(self, reason: str = "before-write") -> None:
        """W7: one shadow snapshot per run before the first file mutation."""
        if self._ws is None:
            return
        try:
            from .. import checkpoints  # noqa: PLC0415

            checkpoints.snapshot(
                self._ws.root, reason=reason, run_id=self._run_id,
                turn_key=self._turn_key,
            )
        except Exception:  # noqa: BLE001 - `warn`/`off` never block a write
            from .. import checkpoints as _checkpoints  # noqa: PLC0415

            if _checkpoints.checkpoint_mode() == "enforce":
                # Safety-first mode: a missing undo point refuses the mutation.
                raise

    # ── context ─────────────────────────────────────────────────────────────
    def _ctx(self, dry_run: bool = False) -> FsContext:
        if self._ws is None:
            raise FsError(WORKSPACE_UNBOUND, "There is no project folder to work in.")
        return FsContext(
            ws=self._ws, binding=self._binding, actor=self._actor,
            config=self._config, dry_run=dry_run,
        )

    @property
    def workspace(self) -> Optional[ResolvedWorkspace]:
        return self._ws

    def info(self) -> dict[str, Any]:
        if self._ws is None:
            raise FsError(WORKSPACE_UNBOUND, "There is no project folder to work in.")
        return {"status": "ok", **workspace_info(self._ws).to_dict()}

    # ── operations ──────────────────────────────────────────────────────────
    def list_directory(self, path: str = ".", **kwargs: Any) -> dict[str, Any]:
        return operations.list_directory(self._ctx(), path, **kwargs)

    def read_file(self, path: str, **kwargs: Any) -> dict[str, Any]:
        return operations.read_file(self._ctx(), path, **kwargs)

    def read_media(self, path: str) -> Optional[dict[str, Any]]:
        """Read a recognised image/PDF, or None for an ordinary file."""
        return operations.read_media(self._ctx(), path)

    def write_file(self, path: str, content: str, **kwargs: Any) -> dict[str, Any]:
        self._before_mutation("before-write")
        return operations.write_file(self._ctx(), path, content, **kwargs)

    def create_file(self, path: str, content: str = "", **kwargs: Any) -> dict[str, Any]:
        self._before_mutation("before-write")
        return operations.create_file(self._ctx(), path, content, **kwargs)

    def delete_file(self, path: str, **kwargs: Any) -> dict[str, Any]:
        self._before_mutation("before-write")
        return operations.delete_file(self._ctx(), path, **kwargs)

    def move_file(self, source: str, destination: str, **kwargs: Any) -> dict[str, Any]:
        self._before_mutation("before-write")
        return operations.move_file(self._ctx(), source, destination, **kwargs)

    def copy_file(self, source: str, destination: str, **kwargs: Any) -> dict[str, Any]:
        self._before_mutation("before-write")
        return operations.copy_file(self._ctx(), source, destination, **kwargs)

    def rename_file(self, path: str, new_name: str, **kwargs: Any) -> dict[str, Any]:
        self._before_mutation("before-write")
        return operations.rename_file(self._ctx(), path, new_name, **kwargs)

    def create_directory(self, path: str, **kwargs: Any) -> dict[str, Any]:
        self._before_mutation("before-write")
        return operations.create_directory(self._ctx(), path, **kwargs)

    def delete_directory(self, path: str, **kwargs: Any) -> dict[str, Any]:
        self._before_mutation("before-write")
        return operations.delete_directory(self._ctx(), path, **kwargs)

    # ── search ──────────────────────────────────────────────────────────────
    def search_text(self, pattern: str, **kwargs: Any) -> dict[str, Any]:
        return search.search_text(self._ctx(), pattern, **kwargs)

    def search_files(self, pattern: str, **kwargs: Any) -> dict[str, Any]:
        return search.search_files(self._ctx(), pattern, **kwargs)

    def find_files(self, **kwargs: Any) -> dict[str, Any]:
        return search.find_files(self._ctx(), **kwargs)

    def search_symbols(self, query: str, **kwargs: Any) -> dict[str, Any]:
        return symbols.search_symbols(self._ctx(), query, **kwargs)

    def find_references(self, symbol: str, **kwargs: Any) -> dict[str, Any]:
        index = self._get_index(create=False)
        # The index is a CACHE, not an authority. When no watcher is keeping it
        # current, refresh it incrementally before trusting it so a file changed
        # since the last indexing pass is never invisible. When a watcher IS
        # active it already re-indexes each change, so no extra walk is needed.
        kwargs.setdefault("refresh_index", self._watcher is None)
        return symbols.find_references(
            self._ctx(), symbol, index=index, root=self._ws.root if self._ws else None, **kwargs,
        )

    # ── editing ─────────────────────────────────────────────────────────────
    def patch_file(self, path: str, operation: str, **kwargs: Any) -> dict[str, Any]:
        self._before_mutation("before-patch")
        return editing.patch_file(self._ctx(), path, operation, **kwargs)

    def replace_text(self, path: str, old_text: str, new_text: str, **kwargs: Any) -> dict[str, Any]:
        self._before_mutation("before-patch")
        return editing.replace_text(self._ctx(), path, old_text, new_text, **kwargs)

    def insert_text(self, path: str, text: str, **kwargs: Any) -> dict[str, Any]:
        self._before_mutation("before-patch")
        return editing.insert_text(self._ctx(), path, text, **kwargs)

    def delete_text(self, path: str, **kwargs: Any) -> dict[str, Any]:
        self._before_mutation("before-patch")
        return editing.delete_text(self._ctx(), path, **kwargs)

    def apply_diff(self, diff_text: str, **kwargs: Any) -> dict[str, Any]:
        self._before_mutation("before-patch")
        return editing.apply_diff(self._ctx(), diff_text, **kwargs)

    def multi_file_patch(self, patches: list[dict[str, Any]], **kwargs: Any) -> dict[str, Any]:
        self._before_mutation("before-patch")
        return editing.multi_file_patch(self._ctx(), patches, **kwargs)

    # ── metadata ────────────────────────────────────────────────────────────
    def file_metadata(self, path: str) -> dict[str, Any]:
        return metadata.file_metadata(self._ctx().ws, path)

    def file_hash(self, path: str) -> dict[str, Any]:
        return metadata.file_hash(self._ctx().ws, path)

    def file_permissions(self, path: str) -> dict[str, Any]:
        return metadata.file_permissions(self._ctx().ws, path)

    # ── verification ────────────────────────────────────────────────────────
    def verify_file(self, path: str, *, expected_hash: Optional[str] = None) -> dict[str, Any]:
        self._ctx().require("read", path)
        if self._ws is None:
            raise FsError(WORKSPACE_UNBOUND, "There is no project folder to work in.")
        return verify.verify_workspace_file(self._ws, path, expected_hash=expected_hash)

    def validate_changes(self, edits: list[dict[str, Any]]) -> dict[str, Any]:
        result = verify.validate_changes(self._ctx(), edits)
        return {"status": "ok", **result}

    # ── git awareness (additive; never required) ─────────────────────────────
    def git_status(self) -> dict[str, Any]:
        if self._ws is None:
            raise FsError(WORKSPACE_UNBOUND, "There is no project folder to work in.")
        return git_mod.summary(self._ws.root)

    # ── index ───────────────────────────────────────────────────────────────
    def _get_index(self, *, create: bool = True) -> Any:
        if self._index is not None:
            return self._index
        if self._ws is None or not create:
            return None
        try:
            self._index = index_mod.open_index(self._ws)
        except Exception:  # noqa: BLE001 - an index failure must not break a tool
            return None
        return self._index

    def index_workspace(self, *, force: bool = False) -> dict[str, Any]:
        if self._ws is None:
            raise FsError(WORKSPACE_UNBOUND, "There is no project folder to index.")
        index = self._get_index()
        if index is None:
            raise FsError("INDEX_FAILED", "The workspace index isn't available.")
        if force:
            index.clear()
        stats = index.update()
        return {"status": "ok", **index.status(), "update": stats.to_dict()}

    # ── watchers ────────────────────────────────────────────────────────────
    def watch(self, paths: Optional[list[str]] = None, *, recursive: bool = True, start: bool = True) -> dict[str, Any]:
        if self._ws is None:
            raise FsError(WORKSPACE_UNBOUND, "There is no project folder to watch.")
        index = self._get_index(create=False)
        self._watcher = watch.WorkspaceWatcher(
            self._ws, targets=paths, recursive=recursive, index=index,
            on_event=_publish_watch_events,
        )
        if start:
            self._watcher.start()
        return self._watcher.status()

    def poll_watcher(self) -> dict[str, Any]:
        if self._watcher is None:
            return {"status": "ok", "events": [], "count": 0}
        events = self._watcher.poll_once()
        return {"status": "ok", "events": [e.to_dict() for e in events], "count": len(events)}

    # ── smart editing (inspection half) ─────────────────────────────────────
    def inspect(self, path: str, *, query: Optional[str] = None, context_lines: int = 3) -> dict[str, Any]:
        """The deterministic groundwork `edit_file` uses before any patch.

        Returns the file's language, symbols, and — when `query` is given — the
        matching regions with line numbers as candidate anchors. It never
        fabricates an edit: the model decides WHAT to change from this packet.
        """
        ctx = self._ctx()
        read = operations.read_file(ctx, path, limit=operations.MAX_READ_LINES)
        rel = read["path"]
        text = "\n".join(read["lines"])
        language = symbols.detect_language(rel)
        file_symbols = [s.to_dict() for s in symbols.extract_symbols(rel, text, language=language)]
        packet: dict[str, Any] = {
            "status": "ok",
            "path": rel,
            "language": language,
            "startLine": read["startLine"],
            "endLine": read["endLine"],
            "totalLines": read["totalLines"],
            "sha256": read["sha256"],
            "symbols": file_symbols[:100],
        }
        if query:
            needle = query.lower()
            regions = []
            for index, line in enumerate(read["lines"]):
                if needle in line.lower():
                    low = max(0, index - context_lines)
                    high = min(len(read["lines"]), index + context_lines + 1)
                    regions.append({
                        "startLine": low + read["startLine"],
                        "endLine": high + read["startLine"] - 1,
                        "anchor": line.strip()[:200],
                        "context": read["lines"][low:high],
                    })
            packet["regions"] = regions[:20]
            packet["regionCount"] = len(regions)
        return packet


def build_service(
    ws: Optional[ResolvedWorkspace],
    binding: Any,
    actor: Any,
    *,
    config: Optional[dict[str, Any]] = None,
) -> FileSystemService:
    return FileSystemService(ws, binding, actor, config=config)


__all__ = ["FileSystemService", "READ_CAPABILITY", "build_service"]
