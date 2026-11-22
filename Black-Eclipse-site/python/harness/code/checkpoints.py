"""Shadow-git file checkpoints + restore (W7).

One bare repository per project under `.black-eclipse/checkpoints/store`.
Snapshots are ordinary commits under `refs/eclipse/<project-hash>/...`; refs are
the source of truth, so no new database is introduced. A temporary
`GIT_INDEX_FILE` means a snapshot never reads or writes the user's index.

Safety rules:
  * protected/ignored paths are never captured or restored;
  * a restore first takes a `pre-restore` snapshot, so it is itself undoable;
  * partial restore is the default; `all` is explicit and only touches tracked
    files (it never deletes an untracked hand-created file);
  * git is the only backend. If it is unavailable the caller gets
    `CHECKPOINT_UNAVAILABLE`, never a simulated store.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

from ..config.flags import get_flag, get_int
from . import policy
from .project_state import ensure_state_dir


def checkpoint_mode() -> str:
    """The effective mode: off | warn | enforce. `on`/true/1 mean enforce."""
    raw = str(get_flag("HARNESS_FILE_CHECKPOINTS") or "off").strip().lower()
    if raw in ("on", "true", "1", "enable", "enabled"):
        return "enforce"
    if raw in ("off", "false", "0", "disable", "disabled", ""):
        return "off"
    return raw if raw in ("warn", "enforce") else "off"


def checkpoints_enabled() -> bool:
    return checkpoint_mode() != "off"

STATE_RELPATH = ".black-eclipse/checkpoints"
STORE_NAME = "store"
INDEX_NAME = "index"
LAST_NAME = "last.json"
EXCLUDES_NAME = "excludes"
MAX_DIFF_CHARS = 200_000
GIT_TIMEOUT_S = 60
CHECKPOINT_ID_RE = re.compile(r"^cp_[0-9]{8}T[0-9]{6}Z_[0-9a-f]{8}$")

EXCLUDES = """\
# Eclipse shadow checkpoints. This file is store-local; it never edits the
# project's .gitignore.
.git/
.black-eclipse/
node_modules/
.next/
.next-dev/
dist/
build/
target/
out/
coverage/
__pycache__/
*.db
*.db-*
*.sqlite
*.sqlite3
.env
.env.*
*.pem
*.key
*.p12
*.zip
*.tar
*.gz
*.png
*.jpg
*.jpeg
*.webp
*.woff2
"""

# Commands that can plausibly write. Conservative on purpose: a false positive
# costs one snapshot; a false negative loses an undo point.
_WRITE_COMMAND_RE = re.compile(
    r"(^|[\s;&|])(rm|del|erase|rmdir|mv|move|cp|copy|tee|touch|mkdir|md|"
    r"sed\s+-i|perl\s+-i|chmod|chown|git\s+(reset|checkout|restore|clean|"
    r"rebase|merge|pull|apply|am|stash)|npm\s+(install|ci|uninstall)|"
    r"pnpm\s+(install|add|remove)|yarn\s+(add|remove)|pip\s+(install|uninstall)|"
    r"cargo\s+(build|clean)|go\s+generate|make|cmake|dotnet\s+(build|clean))\b",
    re.IGNORECASE,
)


class CheckpointError(RuntimeError):
    """A structured checkpoint failure. `code` is stable and testable."""

    def __init__(self, code: str, reason: str) -> None:
        super().__init__(reason)
        self.code = code
        self.reason = reason


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _project_root(root: Any) -> Path:
    try:
        candidate = Path(os.path.realpath(str(root)))
    except (OSError, ValueError) as exc:
        raise CheckpointError("CHECKPOINT_UNAVAILABLE", "the project folder is not usable") from exc
    if not candidate.is_dir():
        raise CheckpointError("CHECKPOINT_UNAVAILABLE", "the project folder is not available")
    return candidate


def _project_hash(root: Path) -> str:
    return hashlib.sha256(str(root).casefold().encode("utf-8")).hexdigest()[:16]


def _state_dir(root: Path) -> Path:
    base = Path(ensure_state_dir(root) or (root / ".black-eclipse")).resolve()
    path = base / "checkpoints"
    path.mkdir(parents=True, exist_ok=True)
    return path


def store_path(root: Path) -> Path:
    return _state_dir(root) / STORE_NAME


def _excludes_path(root: Path) -> Path:
    path = _state_dir(root) / EXCLUDES_NAME
    try:
        if not path.exists() or path.read_text(encoding="utf-8") != EXCLUDES:
            path.write_text(EXCLUDES, encoding="utf-8")
    except OSError:
        pass
    return path


def _git_env(root: Path, *, index: Optional[Path] = None) -> dict[str, str]:
    env = {
        "GIT_DIR": str(store_path(root)),
        "GIT_WORK_TREE": str(root),
        "GIT_AUTHOR_NAME": "Eclipse",
        "GIT_AUTHOR_EMAIL": "eclipse@localhost",
        "GIT_COMMITTER_NAME": "Eclipse",
        "GIT_COMMITTER_EMAIL": "eclipse@localhost",
        "GIT_TERMINAL_PROMPT": "0",
    }
    if index is not None:
        env["GIT_INDEX_FILE"] = str(index)
    # Do not inherit credentials or a caller's git config unexpectedly.
    for key in ("GIT_CONFIG", "GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM"):
        env.pop(key, None)
    return env


def _run(root: Path, args: Iterable[str], *, index: Optional[Path] = None,
         timeout: int = GIT_TIMEOUT_S) -> subprocess.CompletedProcess:
    argv = ("git", "-c", "core.longpaths=true", "-c",
            f"core.excludesFile={_excludes_path(root)}", *args)
    try:
        return subprocess.run(
            list(argv), cwd=str(root), shell=False, capture_output=True,
            timeout=timeout, env=_git_env(root, index=index),
        )
    except FileNotFoundError as exc:
        raise CheckpointError("CHECKPOINT_UNAVAILABLE", "Git is not available for file checkpoints.") from exc
    except subprocess.SubprocessError as exc:
        raise CheckpointError("CHECKPOINT_UNAVAILABLE", f"Git could not read the project: {exc}") from exc


def _ensure_store(root: Path) -> None:
    store = store_path(root)
    if (store / "HEAD").exists():
        return
    store.parent.mkdir(parents=True, exist_ok=True)
    if store.exists():
        shutil.rmtree(store, ignore_errors=True)
    try:
        proc = subprocess.run(
            ["git", "init", "--bare", str(store)], shell=False,
            capture_output=True, timeout=GIT_TIMEOUT_S,
            env={k: v for k, v in os.environ.items() if k.startswith("GIT_") is False},
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise CheckpointError("CHECKPOINT_UNAVAILABLE", f"Could not create the checkpoint store: {exc}") from exc
    if proc.returncode != 0:
        raise CheckpointError("CHECKPOINT_UNAVAILABLE", "Could not create the checkpoint store.")


def _dir_size(path: Path) -> int:
    total = 0
    for dirpath, _dirnames, filenames in os.walk(path):
        for name in filenames:
            try:
                total += os.path.getsize(os.path.join(dirpath, name))
            except OSError:
                continue
    return total


def _last_map(root: Path) -> dict[str, str]:
    try:
        return json.loads((_state_dir(root) / LAST_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _write_last(root: Path, mapping: dict[str, str]) -> None:
    path = _state_dir(root) / LAST_NAME
    try:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(mapping, sort_keys=True), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass


def _turn_key(run_id: str, turn_key: str) -> str:
    return f"{run_id or 'run'}:{turn_key or run_id or 'turn'}"


def _notify(event_type: str, data: dict[str, Any], *, run_id: str = "") -> None:
    """W8 telemetry for this store. Fail-open; it owns no bus."""
    try:
        from ..observability.bridge import publish  # noqa: PLC0415

        publish(event_type, data, run_id=run_id)
    except Exception:  # noqa: BLE001 - a signal must never fail a snapshot
        pass


def snapshot(root: Any, *, reason: str = "before-write", run_id: str = "",
             turn_key: str = "", force: bool = False) -> Optional[dict[str, Any]]:
    """Capture the project once per turn, reporting created/failed to W8."""
    try:
        result = _snapshot_impl(
            root, reason=reason, run_id=run_id, turn_key=turn_key, force=force,
        )
    except CheckpointError as exc:
        _notify("checkpoint.failed", {
            "op": "failed", "reason": exc.code, "code": exc.code,
            "message": exc.reason,
        }, run_id=run_id)
        raise
    except Exception as exc:  # noqa: BLE001 - report, then preserve the failure
        _notify("checkpoint.failed", {
            "op": "failed", "reason": type(exc).__name__, "code": "CHECKPOINT_FAILED",
        }, run_id=run_id)
        raise
    if result is not None:
        _notify("checkpoint.created", {
            "op": "created", "reason": result.get("reason") or reason,
            "files": result.get("files"), "bytes": result.get("bytes"),
        }, run_id=run_id)
    return result


def _snapshot_impl(root: Any, *, reason: str = "before-write", run_id: str = "",
                   turn_key: str = "", force: bool = False) -> Optional[dict[str, Any]]:
    """Capture the project once per turn. Returns the checkpoint metadata.

    Returns None when checkpoints are off. Raises `CheckpointError` when the
    store is unusable or full.
    """
    if not checkpoints_enabled():
        return None
    root_path = _project_root(root)
    key = _turn_key(run_id, turn_key)
    if not force:
        previous = _last_map(root_path).get(key)
        if previous:
            existing = find(root_path, previous, limit=1)
            if existing:
                return existing[0]
    _ensure_store(root_path)
    cap = max(50, get_int("HARNESS_FILE_CHECKPOINT_MAX_MB")) * 1024 * 1024
    if _dir_size(store_path(root_path)) > cap:
        prune(root_path)
        if _dir_size(store_path(root_path)) > cap:
            raise CheckpointError(
                "CHECKPOINT_STORE_FULL",
                "The checkpoint store is full. Restore or prune older snapshots first.",
            )
    index_dir = _state_dir(root_path) / INDEX_NAME
    index_dir.mkdir(parents=True, exist_ok=True)
    index = index_dir / f"{uuid.uuid4().hex}.index"
    try:
        _run(root_path, ["add", "-A", "--", "."], index=index)
        tree = _run(root_path, ["write-tree"], index=index)
        if tree.returncode != 0:
            raise CheckpointError("CHECKPOINT_FAILED", "The project could not be captured.")
        tree_sha = tree.stdout.decode("utf-8", "replace").strip()
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        checkpoint_id = f"cp_{stamp}_{uuid.uuid4().hex[:8]}"
        message = (
            f"eclipse-checkpoint {checkpoint_id}\n\n"
            f"runId={run_id or '-'}\n"
            f"turnKey={turn_key or '-'}\n"
            f"reason={reason or 'before-write'}\n"
            f"createdAt={_now_iso()}\n"
        )
        commit = _run(root_path, ["commit-tree", tree_sha, "-m", message], index=index)
        if commit.returncode != 0:
            raise CheckpointError("CHECKPOINT_FAILED", "The project could not be captured.")
        commit_sha = commit.stdout.decode("utf-8", "replace").strip()
        ref = f"refs/eclipse/{_project_hash(root_path)}/{stamp}-{checkpoint_id}"
        update = _run(root_path, ["update-ref", ref, commit_sha], index=index)
        if update.returncode != 0:
            raise CheckpointError("CHECKPOINT_FAILED", "The snapshot could not be recorded.")
        files = _run(root_path, ["ls-tree", "-r", "--name-only", commit_sha], index=index)
        count = len([line for line in files.stdout.decode("utf-8", "replace").splitlines() if line])
        mapping = _last_map(root_path)
        mapping[key] = checkpoint_id
        _write_last(root_path, mapping)
        # Keep the ref count bounded without paying for a prune on every write.
        try:
            keep = max(1, get_int("HARNESS_FILE_CHECKPOINT_RETENTION"))
            if len(_refs(root_path)) > keep + 5:
                prune(root_path)
        except Exception:  # noqa: BLE001 - retention must not lose the snapshot
            pass
        return {
            "id": checkpoint_id, "ref": ref, "commit": commit_sha, "reason": reason,
            "runId": run_id, "turnKey": turn_key, "createdAt": _now_iso(),
            "files": count, "projectHash": _project_hash(root_path),
        }
    finally:
        try:
            index.unlink()
        except OSError:
            pass


def _worktree_tree(root: Path) -> str:
    """A temporary tree for the CURRENT work tree (no permanent index)."""
    index = _state_dir(root) / INDEX_NAME / f"{uuid.uuid4().hex}.index"
    try:
        _run(root, ["add", "-A", "--", "."], index=index)
        tree = _run(root, ["write-tree"], index=index)
        if tree.returncode != 0:
            raise CheckpointError("CHECKPOINT_FAILED", "The current project state could not be read.")
        return tree.stdout.decode("utf-8", "replace").strip()
    finally:
        try:
            index.unlink()
        except OSError:
            pass


def _refs(root: Path) -> list[str]:
    _ensure_store(root)
    proc = _run(root, ["for-each-ref", "--format=%(refname)", f"refs/eclipse/{_project_hash(root)}/"])
    if proc.returncode != 0:
        return []
    return [line for line in proc.stdout.decode("utf-8", "replace").splitlines() if line]


def _parse_ref(ref: str, commit: Optional[str] = None) -> dict[str, Any]:
    name = ref.rsplit("/", 1)[-1]
    # <stamp>-cp_<stamp>_<hex>
    checkpoint_id = name.split("-", 1)[1] if "-" in name else name
    return {
        "id": checkpoint_id,
        "ref": ref,
        "commit": commit or "",
    }


def list_checkpoints(root: Any, *, limit: int = 50) -> list[dict[str, Any]]:
    if not checkpoints_enabled():
        return []
    root_path = _project_root(root)
    refs = _refs(root_path)
    out: list[dict[str, Any]] = []
    for ref in refs[-max(1, min(int(limit), 500)):][::-1]:
        commit = _run(root_path, ["rev-parse", ref])
        sha = commit.stdout.decode("utf-8", "replace").strip()
        if commit.returncode != 0 or not sha:
            continue
        meta = _parse_ref(ref, sha)
        body = _run(root_path, ["log", "-1", "--format=%B", sha])
        text = body.stdout.decode("utf-8", "replace")
        for line in text.splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                if key in ("runId", "turnKey", "reason", "createdAt"):
                    meta[key] = value
        files = _run(root_path, ["ls-tree", "-r", "--name-only", sha])
        meta["files"] = len([x for x in files.stdout.decode("utf-8", "replace").splitlines() if x])
        out.append(meta)
    # Refs are stamped to the SECOND; two snapshots in the same second must
    # still list newest-first (the commit body records microseconds).
    out.sort(key=lambda item: str(item.get("createdAt") or ""), reverse=True)
    return out


def find(root: Any, checkpoint_id: str, *, limit: int = 500) -> list[dict[str, Any]]:
    return [cp for cp in list_checkpoints(root, limit=limit) if cp.get("id") == checkpoint_id]


def _safe_paths(root: Path, paths: Optional[Iterable[str]]) -> list[str]:
    if not paths:
        return []
    safe: list[str] = []
    for raw in paths:
        rel = str(raw or "").replace("\\", "/").lstrip("./")
        if not rel or rel.startswith("../") or "/../" in rel:
            continue
        if policy.is_protected(rel) or policy.is_ignored(rel):
            continue
        safe.append(rel)
    return safe


def diff(root: Any, checkpoint_id: str, *, paths: Optional[Iterable[str]] = None) -> dict[str, Any]:
    root_path = _project_root(root)
    matches = find(root_path, checkpoint_id)
    if not matches:
        raise CheckpointError("CHECKPOINT_NOT_FOUND", "That snapshot is not on record.")
    commit = matches[0]["commit"]
    safe = _safe_paths(root_path, paths)
    current = _worktree_tree(root_path)
    args = ["diff", "--no-color", "--no-ext-diff", commit, current, "--", *(safe or ["."])]
    proc = _run(root_path, args)
    text = proc.stdout.decode("utf-8", "replace")
    truncated = len(text) > MAX_DIFF_CHARS
    if truncated:
        text = text[:MAX_DIFF_CHARS]
    return {
        "available": True, "checkpointId": checkpoint_id, "commit": commit,
        "unified": text, "truncated": truncated, "paths": safe,
    }


def command_may_write(command: str) -> bool:
    """Conservative: does this shell command plausibly mutate files?"""
    text = str(command or "")
    if not text.strip():
        return False
    if ">" in text or ">>" in text:
        return True
    return bool(_WRITE_COMMAND_RE.search(text))


def _changed(root: Path, commit: str) -> list[tuple[str, str]]:
    current = _worktree_tree(root)
    proc = _run(root, ["diff", "--name-status", "--no-renames", commit, current, "--"])
    if proc.returncode != 0:
        return []
    out: list[tuple[str, str]] = []
    for line in proc.stdout.decode("utf-8", "replace").splitlines():
        parts = line.split("\t", 1)
        if len(parts) != 2:
            continue
        status, path = parts[0][:1], parts[1].replace("\\", "/")
        out.append((status, path))
    return out


def change_paths(root: Any, checkpoint_id: str) -> list[dict[str, str]]:
    """The tracked files that differ from a snapshot (status + path)."""
    root_path = _project_root(root)
    matches = find(root_path, checkpoint_id)
    if not matches:
        raise CheckpointError("CHECKPOINT_NOT_FOUND", "That snapshot is not on record.")
    return [
        {"status": status, "path": path}
        for status, path in _changed(root_path, matches[0]["commit"])
        if not policy.is_protected(path) and not policy.is_ignored(path)
    ]


def preview_restore(root: Any, checkpoint_id: str, *, paths: Optional[Iterable[str]] = None,
                    all: bool = False) -> dict[str, Any]:
    changes = change_paths(root, checkpoint_id)
    if paths:
        wanted = set(_safe_paths(_project_root(root), paths))
        changes = [c for c in changes if c["path"] in wanted]
    elif not all:
        changes = []
    return {
        "checkpointId": checkpoint_id,
        "all": bool(all),
        "changes": changes,
        "count": len(changes),
        "diff": diff(root, checkpoint_id, paths=paths)["unified"][:60_000],
    }


def restore(root: Any, checkpoint_id: str, *, paths: Optional[Iterable[str]] = None,
            all: bool = False, run_id: str = "", turn_key: str = "") -> dict[str, Any]:
    """Restore named paths (default) or every tracked change (`all`).

    A `pre-restore` snapshot is taken first. Protected/ignored paths are skipped.
    """
    if not checkpoints_enabled():
        raise CheckpointError("CHECKPOINT_DISABLED", "File checkpoints are off.")
    root_path = _project_root(root)
    matches = find(root_path, checkpoint_id)
    if not matches:
        raise CheckpointError("CHECKPOINT_NOT_FOUND", "That snapshot is not on record.")
    commit = matches[0]["commit"]
    # The undo of the undo.
    pre = snapshot(root_path, reason="pre-restore", run_id=run_id,
                   turn_key=turn_key or uuid.uuid4().hex, force=True)
    requested = _safe_paths(root_path, paths)
    changed = _changed(root_path, commit)
    skipped: list[str] = []
    selected: list[tuple[str, str]] = []
    if requested:
        wanted = set(requested)
        for status, path in changed:
            if path in wanted:
                selected.append((status, path))
        skipped = [p for p in requested if p not in {path for _s, path in selected}]
    elif all:
        for status, path in changed:
            if policy.is_protected(path) or policy.is_ignored(path):
                skipped.append(path)
            else:
                selected.append((status, path))
    else:
        raise CheckpointError(
            "CHECKPOINT_PATH_REQUIRED",
            "Choose the file(s) to restore, or pass all=true for every tracked change.",
        )
    restored: list[str] = []
    for status, path in selected:
        target = root_path / path
        # `A` means the path exists now but not in the snapshot: removing it is
        # the restore. `D`/`M` means the snapshot has the content.
        if status == "A":
            try:
                if target.is_file() or target.is_symlink():
                    target.unlink()
                    restored.append(path)
            except OSError:
                skipped.append(path)
            continue
        proc = _run(root_path, ["checkout", commit, "--", path])
        if proc.returncode == 0:
            restored.append(path)
        else:
            skipped.append(path)
    return {
        "status": "restored", "checkpointId": checkpoint_id,
        "preRestoreId": (pre or {}).get("id"), "restored": restored,
        "skipped": skipped, "requestedAll": bool(all),
    }


def prune(root: Any) -> dict[str, Any]:
    """Delete old refs beyond the retention cap and free objects."""
    if not checkpoints_enabled():
        return {"pruned": 0}
    root_path = _project_root(root)
    keep = max(1, get_int("HARNESS_FILE_CHECKPOINT_RETENTION"))
    refs = _refs(root_path)
    remove = refs[:-keep] if len(refs) > keep else []
    removed: list[str] = []
    for ref in remove:
        proc = _run(root_path, ["update-ref", "-d", ref])
        if proc.returncode == 0:
            removed.append(ref.rsplit("/", 1)[-1])
    if removed:
        _run(root_path, ["gc", "--prune=now", "--quiet"])
        _notify("checkpoint.pruned", {"op": "pruned", "removed": len(removed)})
    return {"pruned": len(removed), "removed": removed[:20]}


__all__ = [
    "CheckpointError",
    "MAX_DIFF_CHARS",
    "STATE_RELPATH",
    "change_paths",
    "command_may_write",
    "diff",
    "find",
    "list_checkpoints",
    "preview_restore",
    "prune",
    "restore",
    "snapshot",
    "store_path",
]
