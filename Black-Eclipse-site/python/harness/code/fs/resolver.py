"""Path resolution — the ONE place an fs path becomes a confined real path.

This module adds no second confinement implementation. Every path goes through
`code/workspace.confine`, which delegates to `policy.resolve_within`; the only
work here is:

  * mapping a `WorkspaceDenial` to the standard `FsError` vocabulary;
  * supporting a destination path that does not exist yet (a write target);
  * the relative-path helpers a tool needs for display and scope checks.

A grep for `realpath` under `python/harness/code/` still returns only
`workspace.py` and `policy.py` — this module never resolves a path itself.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from .. import project_policy
from ..workspace import ResolvedWorkspace, WorkspaceDenial, confine, is_binary
from . import errors as err

MAX_PATH_CHARS = 1024


@dataclass(frozen=True)
class WorkspaceInfo:
    """The agent-facing view of the workspace boundary."""

    id: str
    root: str
    strategy: str
    base_revision: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "root": self.root,
            "strategy": self.strategy,
            "baseRevision": self.base_revision,
        }


def _map_denial(denial: WorkspaceDenial) -> err.FsError:
    mapping = {
        "PATH_TRAVERSAL": "PATH_TRAVERSAL",
        "PATH_OUTSIDE_WORKSPACE": "PATH_NOT_ALLOWED",
        "OUTSIDE_WORKSPACE": "PATH_NOT_ALLOWED",
        "SYMLINK_ESCAPE": "SYMLINK_ESCAPE",
        "PROTECTED_PATH": "PROTECTED_PATH",
        "IGNORED_PATH": "IGNORED_PATH",
        "NOT_FOUND": err.PATH_NOT_FOUND,
    }
    return err.FsError(mapping.get(denial.code, err.INVALID_PATH), denial.reason, path=denial.path)


def workspace_info(ws: ResolvedWorkspace) -> WorkspaceInfo:
    return WorkspaceInfo(id=ws.id, root=str(ws.root), strategy=ws.strategy, base_revision=ws.base_revision)


def resolve(
    ws: ResolvedWorkspace, rel_path: str, *, for_write: bool = False,
    for_change: bool = False, allow_ignored: bool = False,
) -> Path:
    """Resolve a model-supplied path inside the workspace.

    `for_write=True` permits a target whose parent exists but whose own path does
    not (a new file), while still refusing traversal, an escape, a symlink/junction
    escape, a protected path and an ignored path. Anything else raises `FsError`.
    `for_change=True` marks a DESTRUCTIVE source (delete/move) so a project's own
    deny list applies there too.
    """
    text = str(rel_path or "").strip()
    if not text:
        raise err.FsError(err.INVALID_PATH, "No path was given.", path=text)
    if "\x00" in text:
        raise err.FsError(err.INVALID_PATH, "That path is not valid.", path=text)
    if len(text) > MAX_PATH_CHARS:
        raise err.FsError(err.TOO_LARGE, "That path is too long.", path=text[:80])
    try:
        target = confine(ws, text, for_write=for_write, allow_ignored=allow_ignored)
    except WorkspaceDenial as denial:
        raise _map_denial(denial) from denial
    if (for_write or for_change) and project_policy.is_denied(ws.root, relative(ws, target)):
        raise err.FsError(
            err.PROTECTED_PATH,
            "This project protects that path, so I can't change it.",
            path=relative(ws, target),
        )
    return target


def relative(ws: ResolvedWorkspace, path: Path) -> str:
    """The workspace-relative posix path for display, or the absolute path.

    Only a path INSIDE the workspace is expected here; an outside path (which
    only appears for an internal value, never a model input) is returned as-is.
    """
    try:
        return path.relative_to(ws.root).as_posix()
    except ValueError:
        return str(path)


def is_inside(ws: ResolvedWorkspace, path: Path) -> bool:
    try:
        path.relative_to(ws.root)
        return True
    except ValueError:
        return False


def is_binary_file(path: Path) -> bool:
    """Binary detection, using the workspace's single probe implementation."""
    if not path.exists() or not path.is_file():
        return False
    return is_binary(path)


def path_exists(ws: ResolvedWorkspace, rel_path: str) -> bool:
    try:
        return resolve(ws, rel_path).exists()
    except err.FsError:
        return False


def require_regular_file(ws: ResolvedWorkspace, rel_path: str, *, allow_ignored: bool = False) -> Path:
    """Resolve and require a regular, non-symlink file (metadata/hash/read)."""
    target = resolve(ws, rel_path, allow_ignored=allow_ignored)
    if target.is_dir():
        raise err.FsError(err.NOT_A_FILE, "That is a folder, not a file.", path=relative(ws, target))
    if not target.exists():
        raise err.FsError(err.PATH_NOT_FOUND, "That file isn't there.", path=rel_path)
    if not target.is_file():
        raise err.FsError(err.NOT_A_FILE, "That isn't a regular file.", path=rel_path)
    return target


def require_directory(ws: ResolvedWorkspace, rel_path: str) -> Path:
    target = resolve(ws, rel_path)
    if not target.exists():
        raise err.FsError(err.PATH_NOT_FOUND, "That folder isn't there.", path=rel_path)
    if not target.is_dir():
        raise err.FsError(err.NOT_A_DIRECTORY, "That isn't a folder.", path=rel_path)
    return target


def default_workspace(ws: Optional[ResolvedWorkspace]) -> ResolvedWorkspace:
    if ws is None:
        raise err.FsError(err.WORKSPACE_UNBOUND, "There is no project folder to work in.")
    return ws


__all__ = [
    "MAX_PATH_CHARS",
    "WorkspaceInfo",
    "default_workspace",
    "is_binary_file",
    "is_inside",
    "path_exists",
    "relative",
    "require_directory",
    "require_regular_file",
    "resolve",
    "workspace_info",
]
