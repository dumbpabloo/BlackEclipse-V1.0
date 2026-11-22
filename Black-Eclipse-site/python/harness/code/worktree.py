"""Disposable code workspaces (Phase 2).

A code workspace is a throwaway copy of the repository the agent may write to.
The operator's checkout is never a target: `allocate` creates the workspace, and
`release` can only delete something inside the scratch root — a path check, not a
convention. No function here takes the checkout as an argument; the root is
resolved internally from `workspace.default_root()` (the same server-resolved
root the Phase-1 navigator uses), so no caller can name a different directory.

Two strategies, chosen honestly and reported:

  * "worktree" — `git worktree add --detach <dir> <base>` under
    <root>/.black-eclipse/code-workspaces/<id>. The repo's .gitignore already
    ignores /.black-eclipse/, so a leaked worktree is never a tracked artifact. A
    detached HEAD means no branch is created, moved or deleted.
  * "snapshot" — a bounded recursive copy of the tracked tree, used when
    `git worktree` is unavailable (no git on PATH, a non-git root). It is SLOWER
    and uses more disk, and the strategy string on the returned record says
    "snapshot" — the runtime never claims a git worktree it did not create.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from . import policy

SCRATCH_RELPATH = ".black-eclipse/code-workspaces"
MAX_SNAPSHOT_FILES = 20_000
MAX_SNAPSHOT_BYTES = 512 * 1024 * 1024
MAX_DIFF_BYTES = 2 * 1024 * 1024
GIT_WORKTREE_TIMEOUT_S = 60
WORKSPACE_TTL_SECONDS = 24 * 60 * 60

# The ONLY argv this module may run. No model-supplied argument reaches any of
# them; `base_revision` and `dir` come from validated internal values only.
#
# `-c core.longpaths=true` is set on every invocation: on Windows an unconfigured
# git aborts a checkout that contains deep paths (the scaffold-kit templates under
# `code/kits/**` exceed MAX_PATH) with "Filename too long". That failure silently
# demoted every code task to the full-copy "snapshot" fallback, which then refused
# with SNAPSHOT_TOO_LARGE on this repo — so no write ever succeeded. Passing the
# config per-command avoids mutating the operator's global git config.
_GIT: tuple[str, ...] = ("git", "-c", "core.longpaths=true")

# Process-local capability tokens for a freshly allocated scratch workspace.
# A run carries the opaque token, never a path; `claim` pops it so a replay or a
# second run cannot reuse another task's scratch root.
_SCRATCH_TOKENS: dict[str, Path] = {}
_MAX_SCRATCH_TOKENS = 256


def mint_scratch_token(root: Path) -> str:
    token = "cwscratch_" + uuid.uuid4().hex
    if len(_SCRATCH_TOKENS) >= _MAX_SCRATCH_TOKENS:
        _SCRATCH_TOKENS.pop(next(iter(_SCRATCH_TOKENS)), None)
    _SCRATCH_TOKENS[token] = Path(os.path.realpath(str(root)))
    return token


def claim_scratch(token: str) -> Optional[Path]:
    """Consume a scratch token. One token binds exactly one run."""
    return _SCRATCH_TOKENS.pop(str(token or ""), None)


def peek_scratch(token: str) -> Optional[Path]:
    return _SCRATCH_TOKENS.get(str(token or ""))


GIT_WORKTREE_ARGV: dict[str, tuple[str, ...]] = {
    "add": _GIT + ("worktree", "add", "--detach"),
    "remove": _GIT + ("worktree", "remove", "--force"),
    "rev_parse": _GIT + ("rev-parse", "HEAD"),
}
BASE_REVISION_PATTERN = r"[0-9a-fA-F]{7,40}|HEAD(~\d+)?"

_REVISION_RE = re.compile(rf"^(?:{BASE_REVISION_PATTERN})$")


@dataclass(frozen=True)
class CodeWorkspace:
    id: str                 # "cw_" + 16 hex chars
    root: Path              # <scratch>/<id> — the worktree root
    strategy: str           # "worktree" | "snapshot"
    base_revision: str      # resolved git sha, or the snapshot digest
    created_at: str
    file_count: int = 0
    released_at: Optional[str] = None
    diff_digest: Optional[str] = None    # set by diff(), sha256 of the unified diff
    checkout_root: Optional[Path] = None  # the PROJECT this build was cut from


class WorkspaceError(Exception):
    """A structured workspace failure. `code` is stable and testable.

    Codes: WORKSPACE_ROOT_UNAVAILABLE | GIT_WORKTREE_UNAVAILABLE |
    SNAPSHOT_TOO_LARGE | RELEASE_OUTSIDE_SCRATCH | UNKNOWN_WORKSPACE |
    GIT_FAILED | INVALID_REVISION.
    """

    def __init__(self, code: str, reason: str) -> None:
        super().__init__(reason)
        self.code = code
        self.reason = reason


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _current_root() -> Path:
    """The server-resolved root. Resolved HERE — never an argument to a caller."""
    from .workspace import default_root  # noqa: PLC0415

    root = default_root()
    if root is None:
        raise WorkspaceError(
            "WORKSPACE_ROOT_UNAVAILABLE",
            "there is no project root available for a change to be prepared in",
        )
    return root


def _resolve_allocate_root(root: Optional[Path]) -> Path:
    """The checkout an `allocate` call may cut from.

    The run's resolved root when given (an approved folder the runtime bound),
    else the global default. A scratch folder is never a checkout, so a bad
    binding cannot nest a workspace inside another workspace.
    """
    if root is None:
        return _current_root()
    try:
        candidate = Path(os.path.realpath(str(root)))
    except (OSError, ValueError) as exc:
        raise WorkspaceError("WORKSPACE_ROOT_UNAVAILABLE", "the run's project folder is not a usable path") from exc
    if not candidate.is_dir():
        raise WorkspaceError(
            "WORKSPACE_ROOT_UNAVAILABLE",
            "the run's project folder is no longer available",
        )
    if SCRATCH_RELPATH.replace("\\", "/") in str(candidate).replace("\\", "/"):
        raise WorkspaceError(
            "WORKSPACE_ROOT_UNAVAILABLE",
            "a prepared-change folder cannot also be the project root",
        )
    return candidate


def scratch_root(repo_root: Path) -> Path:
    """<repo_root>/.black-eclipse/code-workspaces — created lazily, 0700 on POSIX.

    Pure helper: it takes a root but is not itself a target; `allocate`/`release`
    resolve the root internally and never accept it from a caller.
    """
    path = Path(repo_root) / SCRATCH_RELPATH
    # A user's repo has no `.black-eclipse` ignore rule; make the state directory
    # self-ignoring so a build never leaves untracked files in their git status.
    from .project_state import ensure_state_dir  # noqa: PLC0415

    ensure_state_dir(repo_root)
    path.mkdir(parents=True, exist_ok=True)
    if os.name == "posix":
        try:
            os.chmod(path, 0o700)
        except OSError:
            pass
    return path


def _run(argv: tuple[str, ...], cwd: Path) -> subprocess.CompletedProcess:
    """Run one fixed argv with no shell. The only child process in this phase."""
    return subprocess.run(
        list(argv),
        cwd=str(cwd),
        shell=False,
        capture_output=True,
        timeout=GIT_WORKTREE_TIMEOUT_S,
        env={k: os.environ[k] for k in policy.SECRET_ENV_ALLOWLIST if k in os.environ},
    )


def _git_available(root: Path) -> bool:
    try:
        proc = _run(("git", "--version"), root)
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0


def _git_head(root: Path) -> str:
    """The git HEAD sha for `root`, or "direct" when git is absent. Never invents."""
    try:
        proc = _run(GIT_WORKTREE_ARGV["rev_parse"], root)
        if proc.returncode == 0:
            text = proc.stdout.decode("utf-8", "replace").strip()
            if re.fullmatch(r"[0-9a-fA-F]{7,40}", text):
                return text
    except (OSError, subprocess.SubprocessError):
        pass
    return "direct"


def _valid_revision(base: str) -> str:
    text = str(base or "HEAD").strip()
    if not _REVISION_RE.match(text):
        raise WorkspaceError("INVALID_REVISION", f"revision {base!r} is not a plain git revision")
    return text


def _stable_id() -> str:
    return "cw_" + uuid.uuid4().hex[:16]


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _manifest(root: Path) -> dict[str, str]:
    """A bounded path→sha256 manifest of the files under `root`, ignore-aware."""
    out: dict[str, str] = {}
    files = 0
    total = 0
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        current = Path(dirpath)
        dirnames[:] = [d for d in dirnames if d not in policy.IGNORED_DIRS and d != ".git"]
        for name in filenames:
            full = current / name
            rel = full.relative_to(root).as_posix()
            if policy.is_ignored(rel):
                continue
            try:
                size = full.stat().st_size
            except OSError:
                continue
            if files >= MAX_SNAPSHOT_FILES or total + size > MAX_SNAPSHOT_BYTES:
                raise WorkspaceError("SNAPSHOT_TOO_LARGE", "the project is too large to copy")
            total += size
            files += 1
            out[rel] = _hash_file(full)
    return out


def _snapshot_copy(source: Path, dest: Path) -> tuple[int, str]:
    """Copy tracked files from `source` into `dest`. Returns (fileCount, digest)."""
    manifest = _manifest(source)
    for rel in manifest:
        src = source / rel
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, target)
    digest = hashlib.sha256(
        json.dumps(manifest, sort_keys=True).encode("utf-8")
    ).hexdigest()
    return len(manifest), digest


def _write_manifest(sidecar: Path, manifest: dict[str, str]) -> None:
    sidecar.write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")


def _read_manifest(sidecar: Path) -> dict[str, str]:
    try:
        return json.loads(sidecar.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def allocate(
    base_revision: str = "HEAD", *, direct: bool = False, root: Optional[Path] = None,
) -> CodeWorkspace:
    """Create a disposable workspace. Deterministic, bounded, honest about strategy.

    With ``direct=True`` the workspace IS the resolved root itself (strategy
    ``"direct"``) — the user chose this folder and approved it, so writes land in
    their real files. ``release`` NEVER deletes a ``"direct"`` workspace. Direct
    mode is opt-in per run and only ever binds a root the runtime already
    resolved and a person already approved.

    ``root`` is the root a RUN resolved (`graph._run_workspace` / the Phase 23
    per-chat binding). It is still never a request-body value: the model cannot
    call this function, and the path comes from `resolve_workspace`, which only
    returns an approved root. Omitted, it falls back to the globally selected
    default exactly as before.

    Otherwise the order of attempts, all inside the scratch root:
      1. `git worktree add --detach <dir> <base>` with cwd=root, no shell.
      2. On failure OR when git is unavailable: a bounded snapshot copy of the
         tree. An honest refusal (`SNAPSHOT_TOO_LARGE`) past the caps — never a
         partial copy silently used as if complete.

    The root is the RUN's resolved root when the caller has one (see ``root``),
    otherwise the internally resolved default; a request body can never name it.
    Tests point the resolver at a temp repo with `BLACK_ECLIPSE_CODE_ROOT`.
    """
    repo_root = _resolve_allocate_root(root)
    if direct:
        base = _valid_revision(base_revision) if _REVISION_RE.match(str(base_revision or "HEAD")) else "HEAD"
        return CodeWorkspace(
            id="cw_direct_" + hashlib.sha256(str(repo_root).encode("utf-8")).hexdigest()[:16],
            root=repo_root,
            strategy="direct",
            base_revision=_git_head(repo_root) if _git_available(repo_root) else "direct",
            created_at=_now_iso(),
            file_count=0,
            checkout_root=repo_root,
        )
    base = _valid_revision(base_revision)
    scratch = scratch_root(repo_root)
    workspace_id = _stable_id()
    dest = scratch / workspace_id
    created_at = _now_iso()

    if _git_available(repo_root):
        try:
            proc = _run(GIT_WORKTREE_ARGV["add"] + (str(dest), base), repo_root)
            if proc.returncode == 0 and dest.is_dir():
                file_count = len(_manifest(dest))
                return CodeWorkspace(
                    id=workspace_id, root=dest, strategy="worktree",
                    base_revision=base, created_at=created_at, file_count=file_count,
                    checkout_root=repo_root,
                )
        except (OSError, subprocess.SubprocessError):
            pass

    # Snapshot fallback — honest about being a copy, bounded.
    try:
        dest.mkdir(parents=True, exist_ok=False)
        file_count, digest = _snapshot_copy(repo_root, dest)
        _write_manifest(scratch / f"{workspace_id}.manifest.json", _manifest(dest))
        return CodeWorkspace(
            id=workspace_id, root=dest, strategy="snapshot",
            base_revision=digest[:40], created_at=created_at, file_count=file_count,
            checkout_root=repo_root,
        )
    except WorkspaceError:
        shutil.rmtree(dest, ignore_errors=True)
        raise
    except OSError as exc:
        shutil.rmtree(dest, ignore_errors=True)
        raise WorkspaceError("GIT_WORKTREE_UNAVAILABLE", f"could not create a workspace: {exc}") from exc


def _workspace_dir(repo_root: Path, workspace_id: str) -> Path:
    scratch = Path(os.path.realpath(scratch_root(repo_root)))
    candidate = Path(os.path.realpath(scratch / str(workspace_id)))
    if candidate != scratch and scratch not in candidate.parents:
        raise WorkspaceError(
            "RELEASE_OUTSIDE_SCRATCH",
            "refused to remove a folder that is not inside the task's own scratch area",
        )
    return candidate


def release(workspace_id: str, *, root: Optional[Path] = None) -> dict[str, Any]:
    """Remove a workspace. Idempotent. Refuses to delete anything outside scratch.

    `realpath(workspace_root)` must be under `realpath(scratch_root)`, else
    `RELEASE_OUTSIDE_SCRATCH` and nothing is deleted — the check that makes
    "disposable" safe rather than hopeful. A git worktree is deregistered with
    `git worktree remove --force`, then removed with `shutil.rmtree` if it still
    exists. An already-released workspace returns `{"alreadyReleased": True}`.

    ``root`` is the project the workspace was cut from (recorded on its row), so a
    task that belongs to a non-selected project is still cleaned up there.
    """
    repo_root = _resolve_allocate_root(root)
    if str(workspace_id).startswith("cw_direct_"):
        # A direct workspace IS the user's real folder. Never delete it.
        return {"released": True, "alreadyReleased": True, "workspaceId": workspace_id,
                "direct": True}
    target = _workspace_dir(repo_root, workspace_id)
    if not target.exists():
        return {"released": True, "alreadyReleased": True, "workspaceId": workspace_id}
    if _git_available(repo_root):
        try:
            _run(GIT_WORKTREE_ARGV["remove"] + (str(target),), repo_root)
        except (OSError, subprocess.SubprocessError):
            pass
    if target.exists():
        shutil.rmtree(target, ignore_errors=True)
    sidecar = scratch_root(repo_root) / f"{workspace_id}.manifest.json"
    try:
        sidecar.unlink()
    except OSError:
        pass
    return {"released": True, "alreadyReleased": False, "workspaceId": workspace_id}


def _unified_from_manifests(before: dict[str, str], after: dict[str, str]) -> tuple[str, list[str], list[str], list[str]]:
    """A synthesized unified diff for a snapshot workspace (no git index)."""
    added = sorted(set(after) - set(before))
    deleted = sorted(set(before) - set(after))
    modified = sorted(p for p in (set(before) & set(after)) if before[p] != after[p])
    lines: list[str] = []
    for path in added:
        lines.append(f"--- /dev/null\n+++ b/{path}\n@@ added @@")
    for path in deleted:
        lines.append(f"--- a/{path}\n+++ /dev/null\n@@ deleted @@")
    for path in modified:
        lines.append(f"--- a/{path}\n+++ b/{path}\n@@ modified @@")
    return "\n".join(lines), added, modified, deleted


def _count_hunks(unified: str) -> tuple[int, int]:
    additions = sum(1 for line in unified.splitlines() if line.startswith("+") and not line.startswith("+++"))
    deletions = sum(1 for line in unified.splitlines() if line.startswith("-") and not line.startswith("---"))
    return additions, deletions


def diff(
    workspace_id: str, base: Optional[str] = None, *, mode: str = "working",
    strategy: str = "worktree", root: Optional[Path] = None,
) -> dict[str, Any]:
    """The unified diff plus added/modified/deleted lists.

    `mode="working"` compares the working tree to `base` (default `HEAD`), which
    is the change the agent has made so far. `mode="committed"` compares
    `base..HEAD`, i.e. what was committed inside the workspace since it was cut.
    A caller that supplies `base` while asking for `working` gets a branch/revision
    comparison (`git diff <rev>`), and the file lists come from that diff rather
    than from the status of the current checkout.

    For a "worktree" workspace the unified diff comes from
    `git -C <root> diff <spec> --no-color --no-ext-diff` (a read-only argv built
    from the workspace record, not from the model). For a "snapshot" workspace
    there is no git object database, so a synthesized diff is produced and the
    result still says `strategy: "snapshot"` — no caller can mistake it for git.

    ``root`` is the project the workspace was cut from; it locates the scratch
    folder when the build belongs to a project other than the selected one.
    """
    if mode not in ("working", "committed"):
        raise WorkspaceError("DIFF_MODE_INVALID", "the change source must be 'working' or 'committed'")
    repo_root = _resolve_allocate_root(root)
    # A direct workspace IS the resolved real root, not a scratch subfolder.
    target = repo_root if strategy == "direct" else _workspace_dir(repo_root, workspace_id)
    if not target.exists():
        raise WorkspaceError("UNKNOWN_WORKSPACE", f"no workspace {workspace_id!r}")
    base_rev = _valid_revision(base) if base else "HEAD"
    spec = f"{base_rev}..HEAD" if mode == "committed" else base_rev

    if strategy == "snapshot":
        if mode == "committed":
            raise WorkspaceError(
                "DIFF_MODE_UNAVAILABLE",
                "a snapshot workspace has no commit history to compare against",
            )
        sidecar = scratch_root(repo_root) / f"{workspace_id}.manifest.json"
        before = _read_manifest(sidecar)
        after = _manifest(target)
        unified, added, modified, deleted = _unified_from_manifests(before, after)
    elif strategy == "direct" and not _git_available(target):
        # A direct workspace is the user's real folder and not a git checkout:
        # there is no base to diff against, so report the working tree honestly.
        unified, added, modified, deleted = "", [], [], []
    else:
        try:
            proc = _run(
                ("git", "-C", str(target), "diff", spec, "--no-color", "--no-ext-diff"),
                repo_root,
            )
            unified = proc.stdout.decode("utf-8", "replace")
        except (OSError, subprocess.SubprocessError) as exc:
            raise WorkspaceError("GIT_FAILED", f"could not read the change: {exc}") from exc
        if mode == "committed":
            named = _run(
                ("git", "-C", str(target), "diff", "--name-status", "--no-renames", spec),
                repo_root,
            )
            added, modified, deleted = _parse_name_status(named.stdout)
        else:
            # A working comparison must include files git does not track yet:
            # `git diff` only sees tracked paths, while a promotion preview has to
            # offer a newly created file too. The union is name-status + `??`.
            named = _run(
                ("git", "-C", str(target), "diff", "--name-status", "--no-renames", spec),
                repo_root,
            )
            added, modified, deleted = _parse_name_status(named.stdout)
            status = _run(
                ("git", "-C", str(target), "status", "--porcelain=v1", "-z"),
                repo_root,
            )
            for path in _untracked_paths(status.stdout):
                if path not in added:
                    added.append(path)

    total_chars = len(unified)
    truncated = total_chars > MAX_DIFF_BYTES
    if truncated:
        unified = unified[:MAX_DIFF_BYTES]
    digest = hashlib.sha256(unified.encode("utf-8")).hexdigest()
    additions, deletions = _count_hunks(unified)
    files_changed = len(added) + len(modified) + len(deleted)
    return {
        "available": True,
        "strategy": strategy,
        "mode": mode,
        "base": spec,
        "workspaceRoot": str(target),
        "unified": unified,
        "truncated": truncated,
        "totalChars": total_chars,
        "added": added,
        "modified": modified,
        "deleted": deleted,
        "digest": digest,
        "filesChanged": files_changed,
        "additions": additions,
        "deletions": deletions,
    }


#: A picture preview is bounded: the UI needs a thumbnail, not a multi-megabyte
#: original, and the route must never become a general file exfiltration door.
MAX_IMAGE_PREVIEW_BYTES = 2 * 1024 * 1024

_IMAGE_SIGNATURES: tuple[tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"BM", "image/bmp"),
)


def _image_mime(data: bytes) -> Optional[str]:
    for signature, mime in _IMAGE_SIGNATURES:
        if data.startswith(signature):
            return mime
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def _image_result(raw: str, data: bytes, mime: str, *, side: str) -> dict[str, Any]:
    return {
        "available": True,
        "path": raw,
        "side": side,
        "mime": mime,
        "size": len(data),
        "bytes": base64.b64encode(data).decode("ascii"),
    }


def _validated_picture_path(relative_path: str) -> str:
    raw = str(relative_path or "").strip().replace("\\", "/")
    if not raw or raw.startswith("/") or re.match(r"^[A-Za-z]:", raw) or ".." in raw.split("/"):
        raise WorkspaceError("PATH_OUTSIDE_WORKSPACE", "the picture path must stay inside the workspace")
    return raw


def _confine_to_workspace(target: Path, raw: str) -> Path:
    base = Path(os.path.realpath(target))
    candidate = Path(os.path.realpath(base / raw))
    if candidate != base and base not in candidate.parents:
        raise WorkspaceError("PATH_OUTSIDE_WORKSPACE", "the picture path must stay inside the workspace")
    return candidate


def _git_before_bytes(target: Path, revision: str, raw: str, max_bytes: int) -> bytes:
    """The base revision's bytes for one path, bounded BEFORE it is read.

    A path with `:` is refused: that is git revision syntax, and this reader
    builds a `rev:path` object name. The size probe prevents a multi-gigabyte
    blob from ever being materialised just to reject it.
    """
    if ":" in raw:
        raise WorkspaceError("PATH_OUTSIDE_WORKSPACE", "the picture path must stay inside the workspace")
    if not _git_available(target):
        raise WorkspaceError("IMAGE_BEFORE_UNAVAILABLE", "there is no history to read the previous picture from")
    spec = f"{revision}:{raw}"
    try:
        probe = _run(("git", "-C", str(target), "cat-file", "-s", spec), target)
    except (OSError, subprocess.SubprocessError) as exc:
        raise WorkspaceError("IMAGE_BEFORE_UNAVAILABLE", "there is no history to read the previous picture from") from exc
    if probe.returncode != 0:
        raise WorkspaceError("IMAGE_BEFORE_UNAVAILABLE", "the previous version is not in the base revision")
    try:
        size = int(probe.stdout.decode("ascii", "replace").strip())
    except ValueError as exc:
        raise WorkspaceError("IMAGE_BEFORE_UNAVAILABLE", "the previous picture size could not be read") from exc
    if size > max_bytes:
        raise WorkspaceError("IMAGE_TOO_LARGE", f"the picture is larger than {max_bytes} bytes")
    try:
        proc = _run(("git", "-C", str(target), "show", spec), target)
    except (OSError, subprocess.SubprocessError) as exc:
        raise WorkspaceError("IMAGE_BEFORE_UNAVAILABLE", "there is no history to read the previous picture from") from exc
    if proc.returncode != 0:
        raise WorkspaceError("IMAGE_BEFORE_UNAVAILABLE", "the previous version is not in the base revision")
    return proc.stdout


def read_image(
    workspace_id: str,
    relative_path: str,
    *,
    side: str = "after",
    base: Optional[str] = None,
    strategy: str = "worktree",
    root: Optional[Path] = None,
    max_bytes: int = MAX_IMAGE_PREVIEW_BYTES,
) -> dict[str, Any]:
    """One bounded picture from a workspace, for the review diff.

    `side="after"` reads the workspace file (what the change produced);
    `side="before"` reads the base revision through git, so the review can show a
    before/after pair. Both sides are confined and bounded identically.

    Confined twice: the path is rejected if absolute or containing `..`, and the
    resolved target must stay inside the workspace. Content is checked by magic
    bytes (an extension is not proof), so this endpoint can only ever return a
    real PNG/JPEG/GIF/WebP/BMP image, never arbitrary file content. The bytes are
    base64 so the JSON route stays the only transport.
    """
    if side not in ("before", "after"):
        raise WorkspaceError("IMAGE_SIDE_INVALID", "the picture side must be 'before' or 'after'")
    raw = _validated_picture_path(relative_path)
    repo_root = _resolve_allocate_root(root)
    target = repo_root if strategy == "direct" else _workspace_dir(repo_root, workspace_id)
    if not target.exists():
        raise WorkspaceError("UNKNOWN_WORKSPACE", f"no workspace {workspace_id!r}")

    if side == "before":
        if strategy == "snapshot":
            # A snapshot keeps hashes, not previous bytes; saying so is honest.
            raise WorkspaceError(
                "IMAGE_BEFORE_UNAVAILABLE",
                "the previous picture is not kept for this kind of workspace",
            )
        revision = _valid_revision(base) if base else "HEAD"
        data = _git_before_bytes(target, revision, raw, max_bytes)
        mime = _image_mime(data)
        if not mime:
            raise WorkspaceError("NOT_AN_IMAGE", "the file is not a PNG/JPEG/GIF/WebP/BMP picture")
        return _image_result(raw, data, mime, side="before")

    candidate = _confine_to_workspace(target, raw)
    if not candidate.is_file():
        raise WorkspaceError("IMAGE_NOT_FOUND", f"no picture at {raw!r} (it may have been removed)")
    size = candidate.stat().st_size
    if size > max_bytes:
        raise WorkspaceError("IMAGE_TOO_LARGE", f"the picture is larger than {max_bytes} bytes")
    data = candidate.read_bytes()
    mime = _image_mime(data)
    if not mime:
        raise WorkspaceError("NOT_AN_IMAGE", "the file is not a PNG/JPEG/GIF/WebP/BMP picture")
    return _image_result(raw, data, mime, side="after")


def _untracked_paths(stdout: bytes) -> list[str]:
    """Paths git reports as untracked (`?? path`) in porcelain -z output."""
    out: list[str] = []
    for part in stdout.split(b"\x00"):
        text = part.decode("utf-8", "replace")
        if text.startswith("?? "):
            out.append(text[3:].replace("\\", "/"))
    return out


def _parse_name_status(stdout: bytes) -> tuple[list[str], list[str], list[str]]:
    """Turn `git diff --name-status` into added/modified/deleted lists."""
    added: list[str] = []
    modified: list[str] = []
    deleted: list[str] = []
    for line in stdout.decode("utf-8", "replace").splitlines():
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        status = parts[0][:1]
        path = parts[-1].replace("\\", "/")
        if status == "A":
            added.append(path)
        elif status == "D":
            deleted.append(path)
        else:
            modified.append(path)
    return added, modified, deleted


def workspace_path(
    workspace_id: str, *, strategy: str = "worktree", root: Optional[Path] = None,
) -> Path:
    """The on-disk folder a workspace owns (for read-only inspection)."""
    repo_root = _resolve_allocate_root(root)
    target = repo_root if strategy == "direct" else _workspace_dir(repo_root, workspace_id)
    if not target.exists():
        raise WorkspaceError("UNKNOWN_WORKSPACE", f"no workspace {workspace_id!r}")
    return target


def _parse_status_lists(stdout: bytes) -> tuple[list[str], list[str], list[str]]:
    added: list[str] = []
    modified: list[str] = []
    deleted: list[str] = []
    parts = [p for p in stdout.split(b"\x00") if p]
    for record in parts:
        text = record.decode("utf-8", "replace")
        if len(text) < 4:
            continue
        status = text[:2]
        path = text[3:]
        if "?" in status or "A" in status:
            added.append(path)
        elif "D" in status:
            deleted.append(path)
        else:
            modified.append(path)
    return added, modified, deleted


def sweep_expired() -> list[str]:
    """Release workspaces past WORKSPACE_TTL_SECONDS. Best-effort, never raises.

    Called from the existing startup lifespan — not a new daemon or thread.
    Returns released ids.
    """
    released: list[str] = []
    try:
        repo_root = _current_root()
        scratch = scratch_root(repo_root)
    except WorkspaceError:
        return released
    now = datetime.now(timezone.utc).timestamp()
    for entry in scratch.iterdir():
        if not entry.is_dir() or not entry.name.startswith("cw_"):
            continue
        try:
            age = now - entry.stat().st_mtime
        except OSError:
            continue
        if age <= WORKSPACE_TTL_SECONDS:
            continue
        try:
            result = release(entry.name)
            if result.get("released"):
                released.append(entry.name)
        except Exception:  # noqa: BLE001 - sweep must never raise
            continue
    return released


__all__ = [
    "BASE_REVISION_PATTERN",
    "CodeWorkspace",
    "GIT_WORKTREE_ARGV",
    "MAX_DIFF_BYTES",
    "MAX_SNAPSHOT_BYTES",
    "MAX_SNAPSHOT_FILES",
    "SCRATCH_RELPATH",
    "WORKSPACE_TTL_SECONDS",
    "WorkspaceError",
    "allocate",
    "claim_scratch",
    "diff",
    "mint_scratch_token",
    "peek_scratch",
    "release",
    "scratch_root",
    "sweep_expired",
]
