"""Workspace resolution, path confinement and bounded reading (Phase 1, read-only).

The ONE place a code path is turned into a real file path. `code/policy.py` owns
the decision rules; this module asks it and maps its answer to a tool error the
model can read. There is no second confinement implementation anywhere — a grep
for `realpath` under python/harness/code/ must return only this file.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Optional

from . import policy
from .policy import PolicyDenial, WorkspaceBinding

# Hard bounds. Every one of these is a refusal or an honest truncation, never a
# silent partial answer.
MAX_READ_LINES = 2_000          # per call
MAX_MATCHES = 200               # per search call
MAX_SCAN_FILES = 5_000          # files opened by one search/docs call
MAX_SCAN_BYTES = 8 * 1024 * 1024
MAX_SEARCH_MS = 5_000           # wall-clock deadline, checked every 200 lines
MAX_PATTERN_CHARS = 200
MAX_LIST_ENTRIES = 1_000
MAX_BINARY_PROBE_BYTES = 8_192
GIT_PROBE_TIMEOUT_S = 10
GIT_PROBE_OUTPUT_CAP_BYTES = 2 * 1024 * 1024

# The complete argv tuples for the fixed git reads. NO model-supplied argument
# ever reaches these — which is exactly why this is not a command broker.
GIT_READONLY_ARGV: dict[str, tuple[str, ...]] = {
    "head": ("git", "rev-parse", "HEAD"),
    "branch": ("git", "rev-parse", "--abbrev-ref", "HEAD"),
    "status": ("git", "status", "--porcelain=v1", "-z", "--untracked-files=all"),
}


@dataclass(frozen=True)
class ResolvedWorkspace:
    """A legal, confirmed workspace root."""

    id: str                      # stable digest of the resolved root path
    root: Path                   # realpath'd, absolute
    base_revision: str           # git HEAD sha, or "unknown" — never invented
    strategy: str                # "checkout" in Phase 1; "worktree" in Phase 2
    binding: WorkspaceBinding    # the Phase-0 dataclass


class WorkspaceDenial(Exception):
    """A refusal with a stable, structured code (never a bare string)."""

    def __init__(self, code: str, reason: str, *, path: str = "") -> None:
        super().__init__(reason)
        self.code = code
        self.reason = reason
        self.path = path


# The fixed check order. `confine` reports the FIRST code that fires, so the
# code a caller sees for a path is deterministic — `jdds.db` is always
# `PROTECTED_PATH`, never `BINARY_FILE`.
CONFINEMENT_ORDER = (
    "PATH_TRAVERSAL",          # a '..' component, or an absolute path
    "PATH_OUTSIDE_WORKSPACE",  # resolved path is not the root or under it
    "SYMLINK_ESCAPE",          # a component (or the target) is a link leaving the root
    "PROTECTED_PATH",          # policy.is_protected(rel)
    "IGNORED_PATH",            # policy.is_ignored(rel) — refused on direct read,
                               #   skipped in listings/searches
    "NOT_FOUND",
    "TOO_LARGE",
    "BINARY_FILE",
)


def _repo_root() -> Optional[str]:
    """The application root that owns the databases, or None.

    Mirrors `AgenticService._project_root()`, which defers to
    `eclipse_memory.config.repo_root()` (the directory containing this harness —
    `Black-Eclipse-site/`, itself inside the git checkout). Kept identical so the
    navigator and the skill project-root agree on one root.
    """
    try:
        from ..eclipse_memory.config import repo_root  # noqa: PLC0415

        resolved = repo_root()
        return resolved if resolved and Path(resolved).is_dir() else None
    except Exception:  # noqa: BLE001 - a missing config is simply no root
        return None


# The process-wide provider for a user-chosen ("trusted") root. The service
# registers this at boot; `default_root` consults it after the explicit env
# override and before the built-in repo root. It is a CALLABLE so a selection
# change takes effect on the next resolution without restarting the runtime. It
# can only ever return a path already approved in `TrustedRoot` (the provider is
# wired to the store's `selected()`), so a chosen folder is still an approved one.
_TRUSTED_ROOT_PROVIDER: Optional[Any] = None
# Resolve an APPROVED root id to its current path. Registered by the service so
# a per-chat binding can pick one of several approved folders without making it
# the global selection. It can only return a row that exists in TrustedRoot.
_TRUSTED_ROOT_LOOKUP: Optional[Any] = None


def set_trusted_root_provider(provider: Optional[Any]) -> None:
    """Register the callable returning the currently selected trusted root path."""
    global _TRUSTED_ROOT_PROVIDER
    _TRUSTED_ROOT_PROVIDER = provider


def set_trusted_root_lookup(provider: Optional[Any]) -> None:
    """Register the callable mapping an approved root id → path (or None)."""
    global _TRUSTED_ROOT_LOOKUP
    _TRUSTED_ROOT_LOOKUP = provider


def trusted_root_path(root_id: Optional[str]) -> Optional[Path]:
    """The path of an APPROVED root id, or None. Never raises."""
    if not root_id or _TRUSTED_ROOT_LOOKUP is None:
        return None
    try:
        found = _TRUSTED_ROOT_LOOKUP(str(root_id))
    except Exception:  # noqa: BLE001 - a broken provider must not bind a run
        return None
    if not found:
        return None
    candidate = Path(str(found))
    return Path(os.path.realpath(candidate)) if candidate.is_dir() else None


def trusted_root_selected() -> bool:
    """True when a user-chosen (trusted) root is the active workspace root.

    The runtime uses this to decide direct mode: a folder a person explicitly
    chose and approved should be edited directly, while the built-in repo root
    stays isolated. Never raises — a broken provider means "not selected".
    """
    if _TRUSTED_ROOT_PROVIDER is None:
        return False
    try:
        return bool(_TRUSTED_ROOT_PROVIDER())
    except Exception:  # noqa: BLE001
        return False


def default_root() -> Optional[Path]:
    """The server-resolved root.

    Resolution order, highest first:
      1. `BLACK_ECLIPSE_CODE_ROOT` (an explicit operator/test override);
      2. the user-selected trusted root, when one is chosen and still exists;
      3. the repo containing the harness (the zero-config default).

    Returns None when none resolves — a missing root is a REFUSAL
    (`WORKSPACE_UNAVAILABLE`), never a guess at a directory.
    """
    override = os.environ.get("BLACK_ECLIPSE_CODE_ROOT")
    if override:
        candidate = Path(override)
        if candidate.is_dir():
            return Path(os.path.realpath(candidate))
        return None
    if _TRUSTED_ROOT_PROVIDER is not None:
        try:
            selected = _TRUSTED_ROOT_PROVIDER()
        except Exception:  # noqa: BLE001 - a broken provider must not break resolution
            selected = None
        if selected:
            candidate = Path(str(selected))
            if candidate.is_dir():
                return Path(os.path.realpath(candidate))
            # A selected root that has vanished falls through to the repo root
            # rather than failing every run; the picker shows it as missing.
    found = _repo_root()
    return Path(os.path.realpath(found)) if found else None


def _stable_id(root: Path) -> str:
    """A stable, content-free digest of the resolved root path."""
    return "ws_" + hashlib.sha256(str(root).encode("utf-8")).hexdigest()[:16]


def _git_head(root: Path) -> str:
    """The git HEAD sha for `root`, or "unknown" — never invented."""
    result = git_probe_kind(root, "head")
    head = result.get("head")
    return str(head) if result.get("available") and head else "unknown"


def _binding_for(root: Path, base_revision: str, strategy: str) -> WorkspaceBinding:
    return WorkspaceBinding(
        id=_stable_id(root),
        root=root,
        base_revision=base_revision,
        strategy=strategy,
    )


def resolve_workspace(
    request_workspace: Any = None,
    root_id: Optional[str] = None,
) -> Optional[ResolvedWorkspace]:
    """Resolve and confirm the workspace for a run.

    Phase 1 binds only the SERVER-RESOLVED root. Phase 23 lets a run name an
    APPROVED root ID (a per-chat project): the service's lookup maps it to the
    real path, so a caller still never supplies a path and the model can never
    widen the workspace. An id that is unknown or revoked yields NO workspace
    (the refusal is honest) — it never silently falls back to another folder.

    An optional `request.workspace` remains a NARROWING/confirmation: its `root`
    must realpath to exactly the resolved root, otherwise the run gets no
    workspace at all.
    """
    if root_id:
        root = trusted_root_path(root_id)
        if root is None:
            return None
    else:
        root = default_root()
    if root is None:
        return None
    requested_root = getattr(request_workspace, "root", None)
    if requested_root:
        try:
            confirmed = Path(os.path.realpath(str(requested_root)))
        except (OSError, ValueError):
            return None
        if confirmed != root:
            return None
    strategy = "checkout"
    base_revision = _git_head(root)
    return ResolvedWorkspace(
        id=_stable_id(root),
        root=root,
        base_revision=base_revision,
        strategy=strategy,
        binding=_binding_for(root, base_revision, strategy),
    )


def resolve_scratch_workspace(scratch_root: Path | str | None) -> Optional[ResolvedWorkspace]:
    """Bind a run to a scratch worktree minted by `worktree.allocate`.

    Only a path *inside* the server-resolved scratch root is accepted; the
    caller supplies the opaque token, and `worktree.claim_scratch` resolves it
    to a path this process created. A missing/foreign path is a refusal.
    """
    if scratch_root is None:
        return None
    base = default_root()
    if base is None:
        return None
    allowed = Path(os.path.realpath(str(base / ".black-eclipse" / "code-workspaces")))
    try:
        candidate = Path(os.path.realpath(str(scratch_root)))
    except (OSError, ValueError):
        return None
    if candidate == allowed or allowed not in candidate.parents:
        return None
    if not candidate.is_dir():
        return None
    base_revision = _git_head(candidate)
    return ResolvedWorkspace(
        id=_stable_id(candidate),
        root=candidate,
        base_revision=base_revision,
        strategy="worktree",
        binding=_binding_for(candidate, base_revision, "worktree"),
    )


def _rel_of(root: Path, resolved: Path) -> str:
    try:
        return resolved.relative_to(root).as_posix()
    except ValueError:
        return ""


def confine(ws: ResolvedWorkspace, rel_path: str, *, for_write: bool = False,
            allow_ignored: bool = False) -> Path:
    """Turn a model-supplied relative path into a confined real path.

    Calls `policy.resolve_within(ws.root, rel_path)` — it does NOT re-implement
    the checks — and maps `PolicyDenial` to a `WorkspaceDenial` 1:1 so a caller
    can assert a DISTINCT code per escape vector. The order is fixed, so `jdds.db`
    reports `PROTECTED_PATH` and never `BINARY_FILE`.
    """
    text = str(rel_path or "").strip()
    if not text:
        raise WorkspaceDenial("NOT_FOUND", "That path is empty, so there's nothing to read.", path=text)
    try:
        resolved = policy.resolve_within(ws.root, text, allow_ignored=allow_ignored)
    except PolicyDenial as denial:
        raise WorkspaceDenial(denial.code, denial.reason, path=text) from denial
    rel = _rel_of(ws.root, resolved)
    if not for_write and not resolved.exists():
        raise WorkspaceDenial("NOT_FOUND", policy._REASON_NOT_FOUND, path=rel or text)
    return resolved


def _scrubbed_env() -> dict[str, str]:
    """A minimal environment built ONLY from `policy.SECRET_ENV_ALLOWLIST`.

    Everything else is dropped — never an `os.environ` passthrough.
    """
    return {name: os.environ[name] for name in policy.SECRET_ENV_ALLOWLIST if name in os.environ}


def _run_git(root: Path, argv: tuple[str, ...]) -> dict[str, Any]:
    """Run ONE fixed, argument-free git read. The only child process here."""
    try:
        completed = subprocess.run(
            list(argv),
            cwd=str(root),
            env=_scrubbed_env(),
            shell=False,
            capture_output=True,
            timeout=GIT_PROBE_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {"available": False, "reason": f"git is not available here ({type(exc).__name__})"}
    out = completed.stdout[:GIT_PROBE_OUTPUT_CAP_BYTES]
    if completed.returncode != 0:
        return {
            "available": False,
            "reason": "this folder is not a git checkout Eclipse can read",
            "stderr": completed.stderr[:512].decode("utf-8", "replace").strip(),
        }
    return {"available": True, "stdout": out}


def git_probe_kind(root: Path, kind: Literal["head", "branch", "status"]) -> dict[str, Any]:
    """Run one fixed git read of `kind` against `root`."""
    argv = GIT_READONLY_ARGV.get(kind)
    if argv is None:
        return {"available": False, "reason": f"unknown probe {kind!r}"}
    raw = _run_git(root, argv)
    if not raw.get("available"):
        return {"available": False, "reason": raw.get("reason", "git is unavailable")}
    stdout: bytes = raw["stdout"]
    if kind == "head":
        return {"available": True, "head": stdout.decode("utf-8", "replace").strip()}
    if kind == "branch":
        return {"available": True, "branch": stdout.decode("utf-8", "replace").strip()}
    return {"available": True, "dirty": _parse_status(stdout)}


def _parse_status(stdout: bytes) -> list[dict[str, str]]:
    """Parse `git status --porcelain=v1 -z` into bounded dirty entries."""
    entries: list[dict[str, str]] = []
    parts = stdout.split(b"\x00")
    index = 0
    while index < len(parts):
        record = parts[index]
        index += 1
        if not record:
            continue
        text = record.decode("utf-8", "replace")
        if len(text) < 4:
            continue
        status = text[:2].strip() or "??"
        path = text[3:]
        # A rename/copy record consumes the NEXT NUL field as the source path.
        if status[0] in ("R", "C") and index < len(parts):
            index += 1
        entries.append({"path": path, "status": status, "owner": "pre-existing"})
        if len(entries) >= MAX_LIST_ENTRIES:
            break
    return entries


def git_probe(ws: ResolvedWorkspace, kind: Literal["head", "branch", "status"]) -> dict[str, Any]:
    """One fixed, argument-free git read. The ONLY child process in Phase 1.

    Returns `{"available": bool, "head", "branch", "dirty": [{"path", "status",
    "owner"}]}`. `owner` is always `"pre-existing"`: nothing this run could have
    changed a file, so a dirty checkout is never misread as the agent's own work.
    When git is absent the result is `{"available": False, "reason": ...}` so the
    tool reports "I can't tell" instead of inventing a branch or revision.
    """
    return git_probe_kind(ws.root, kind)


def walk(
    ws: ResolvedWorkspace, base: str = ".", *, depth: int = 2, max_entries: int = 200,
) -> tuple[list[dict[str, Any]], bool]:
    """A bounded, ignore-aware tree walk (uses `os.walk` with pruning).

    `policy.IGNORED_DIRS` prune the walk; `policy.IGNORED_GLOBS` (incl. `*.db`)
    filter files. Returns `(entries, truncated)`; `truncated` is true when the
    bound stopped the walk. An entry is `{"path", "type", "size"?}`.
    """
    try:
        start = confine(ws, base or ".")
    except WorkspaceDenial as denial:
        raise denial
    if not start.is_dir():
        raise WorkspaceDenial("NOT_FOUND", "That isn't a folder Eclipse can list.", path=base)
    entries: list[dict[str, Any]] = []
    truncated = False
    root_depth = len(start.relative_to(ws.root).parts)
    for dirpath, dirnames, filenames in os.walk(start, followlinks=False):
        current = Path(dirpath)
        current_rel_depth = len(current.relative_to(ws.root).parts)
        # Prune at the directory level so ignored trees are never descended.
        dirnames[:] = [d for d in dirnames if d not in policy.IGNORED_DIRS]
        if current_rel_depth - root_depth >= depth:
            dirnames[:] = []
        for name in dirnames:
            rel = (current / name).relative_to(ws.root).as_posix()
            if policy.is_ignored(rel):
                continue
            entries.append({"path": rel + "/", "type": "dir"})
            if len(entries) >= max_entries:
                return entries, True
        for name in filenames:
            rel = (current / name).relative_to(ws.root).as_posix()
            if policy.is_ignored(rel):
                continue
            entry: dict[str, Any] = {"path": rel, "type": "file"}
            try:
                entry["size"] = (current / name).stat().st_size
            except OSError:
                pass
            entries.append(entry)
            if len(entries) >= max_entries:
                return entries, True
        if len(entries) >= max_entries:
            truncated = True
            break
    return entries, truncated


def is_binary(path: Path) -> bool:
    """Whether a file is binary: a NUL byte in the first probe, or undecodable."""
    try:
        with open(path, "rb") as handle:
            probe = handle.read(MAX_BINARY_PROBE_BYTES)
    except OSError:
        return True
    if b"\x00" in probe:
        return True
    try:
        probe.decode("utf-8")
    except UnicodeDecodeError:
        return True
    return False


__all__ = [
    "CONFINEMENT_ORDER",
    "GIT_READONLY_ARGV",
    "MAX_BINARY_PROBE_BYTES",
    "MAX_LIST_ENTRIES",
    "MAX_MATCHES",
    "MAX_PATTERN_CHARS",
    "MAX_READ_LINES",
    "MAX_SCAN_BYTES",
    "MAX_SCAN_FILES",
    "MAX_SEARCH_MS",
    "ResolvedWorkspace",
    "WorkspaceDenial",
    "resolve_scratch_workspace",
    "confine",
    "default_root",
    "git_probe",
    "git_probe_kind",
    "is_binary",
    "resolve_workspace",
    "walk",
]
