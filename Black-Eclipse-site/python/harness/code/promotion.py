"""Landing a change: confirmed promotion from a disposable copy to the checkout.

Phase 15 (ADR-H119). A code run writes into a DISPOSABLE copy of the project
(`code/worktree.py`); nothing in the runtime ever copies that change back. This
module owns the ONE promotion path:

  1. a PREVIEW names exactly which files would change in the user's real folder,
     with the before/after content hashes and a digest over that exact set;
  2. the runtime records a SINGLE-USE approval bound to that digest;
  3. only an operator's confirmation (the approval) APPLIES it — and only if
     neither the target nor the previewed copy changed since the preview.

It reuses the existing authorities instead of re-implementing them:

  * `worktree.diff` names the changed files in the disposable copy;
  * `code/patch.py` performs the write (path confinement, protected paths, base
    hashes, plan-then-commit atomicity);
  * `mcp/approvals.py` (`request_kind`/`consume_kind`) is the single-use decision.

No git write happens here. A protected path — `code/policy.py` and the other
frozen authorities, `.git`, `.env`, the databases, keys and migrations — can never
be promoted: `policy.is_protected` refuses it in the preview and `patch.apply`
refuses it again at apply time. The target is ALWAYS the server-resolved checkout
root, never a caller-supplied path.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Iterable, Optional

from . import patch as patch_mod
from . import policy
from . import worktree as worktree_mod
from .patch import PatchError, PatchOp
from .policy import WorkspaceBinding
from .workspace import ResolvedWorkspace, default_root

# The approval kind for a promotion. One store (`McpApproval`), a third kind.
PROMOTION_KIND = "code_promote"

# Bounds. A promotion is an exact, bounded set of files, never a bulk move.
MAX_PROMOTION_FILES = 200
MAX_PROMOTION_BYTES = patch_mod.MAX_TOTAL_BYTES_PER_APPLY

# Where the ORIGINAL contents of a promoted change are kept, so it can be put
# back exactly. Inside the repo's gitignored `.black-eclipse/`, never tracked.
ROLLBACK_ROOT_RELPATH = ".black-eclipse/promotions"


class PromotionError(Exception):
    """A promotion was refused. `code` is stable; `reason` is operator-facing.

    Codes: PROMOTION_UNAVAILABLE | PROMOTION_NO_CHANGE | PROMOTION_ALREADY_DIRECT |
    PROMOTION_OUT_OF_SCOPE | PROMOTION_BASE_CHANGED | PROMOTION_PREVIEW_STALE |
    PROMOTION_NOT_APPROVED | PROMOTION_TOO_MANY_FILES | PROMOTION_TOO_LARGE |
    PROMOTION_FAILED.
    """

    def __init__(self, code: str, reason: str) -> None:
        super().__init__(reason)
        self.code = code
        self.reason = reason


def _sha_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _read_text_strict(path: Path) -> str:
    """Read a project file as UTF-8 text, or refuse.

    A promotion is a text change (source, docs, config). A binary or unreadable
    file is refused honestly (`PROMOTION_OUT_OF_SCOPE`) rather than copied byte-
    blind through a text engine.
    """
    try:
        return path.read_text(encoding="utf-8", errors="strict")
    except (UnicodeDecodeError, OSError) as exc:
        raise PromotionError(
            "PROMOTION_OUT_OF_SCOPE",
            f"{path.name} isn't a text change Eclipse can put into your project",
        ) from exc


def promotion_digest(
    files: list[dict[str, Any]], selection: Optional[Iterable[str]] = None,
) -> str:
    """A stable digest over the exact previewed file AND hunk set.

    Order-independent. When `selection` is given, only the named hunks are
    covered, but each selected hunk's before/after content hash is included, so
    an approval cannot be reused for a different slice of the same file.
    """
    chosen = {str(token) for token in selection} if selection is not None else None
    parts: list[str] = []
    for f in sorted(files, key=lambda f: str(f.get("path"))):
        path = str(f.get("path"))
        hunks = [h for h in (f.get("hunks") or []) if isinstance(h, dict)]
        if chosen is None:
            selected = hunks
        else:
            selected = [
                h for h in hunks
                if str(h.get("id")) in {t.split("#", 1)[-1] for t in chosen if t.startswith(path + "#")}
            ]
        token = (
            f"{path}\x00{f.get('op')}\x00{f.get('beforeHash') or ''}\x00{f.get('afterHash') or ''}"
        )
        if hunks:
            token += "\x00" + ";".join(
                f"{h.get('id')}:{h.get('beforeHash')}:{h.get('afterHash')}"
                for h in selected
            )
        parts.append(token)
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


def _hunk_token(path: str, hunk_id: str) -> str:
    return f"{path}#{hunk_id}"


def build_hunks(before: str, after: str) -> list[dict[str, Any]]:
    """Split a text change into grouped hunks with 3 lines of context.

    Each hunk carries the exact before/after slice (line endings preserved), a
    unified header, and content hashes. IDs are positional (`h1`, `h2`, ...) and
    stable for the same before/after pair, so a selection can be re-derived at
    apply time.
    """
    before_lines = before.splitlines(keepends=True)
    after_lines = after.splitlines(keepends=True)
    matcher = SequenceMatcher(None, before_lines, after_lines, autojunk=False)
    hunks: list[dict[str, Any]] = []
    for index, group in enumerate(matcher.get_grouped_opcodes(3), start=1):
        b_start, b_end = group[0][1], group[-1][2]
        a_start, a_end = group[0][3], group[-1][4]
        before_text = "".join(before_lines[b_start:b_end])
        after_text = "".join(after_lines[a_start:a_end])
        preview: list[dict[str, Any]] = []
        for tag, i1, i2, j1, j2 in group:
            if tag == "equal":
                continue
            for line in before_lines[i1:i2][:10]:
                preview.append({"kind": "remove", "text": _preview_line(line)})
            for line in after_lines[j1:j2][:10]:
                preview.append({"kind": "add", "text": _preview_line(line)})
        hunks.append({
            "id": f"h{index}",
            "header": f"@@ -{b_start + 1},{b_end - b_start} +{a_start + 1},{a_end - a_start} @@",
            "beforeStart": b_start + 1,
            "beforeLines": b_end - b_start,
            "afterStart": a_start + 1,
            "afterLines": a_end - a_start,
            "beforeText": before_text,
            "afterText": after_text,
            "beforeHash": _sha_text(before_text),
            "afterHash": _sha_text(after_text),
            "preview": preview[:24],
        })
    return hunks


def _preview_line(line: str) -> str:
    text = line.rstrip("\r\n")
    return text[:200]


def _apply_hunks(before: str, hunks: list[dict[str, Any]], selected: set[str]) -> str:
    """Apply only the selected hunks to `before`, in reverse line order."""
    chosen = [h for h in hunks if str(h.get("id")) in selected]
    if not chosen:
        return before
    lines = before.splitlines(keepends=True)
    for hunk in sorted(chosen, key=lambda h: int(h.get("beforeStart") or 1), reverse=True):
        start = max(0, int(hunk.get("beforeStart") or 1) - 1)
        count = int(hunk.get("beforeLines") or 0)
        current = "".join(lines[start:start + count])
        if current != str(hunk.get("beforeText") or ""):
            raise PromotionError(
                "PROMOTION_PREVIEW_STALE",
                "The prepared change was altered after you reviewed it; review it again.",
            )
        lines[start:start + count] = [str(hunk.get("afterText") or "")]
    return "".join(lines)


def _operator_actor(binding_id: str) -> Any:
    from ..skills.permissions import Actor  # noqa: PLC0415

    # An operator-confirmed write. The `code.write` verb comes from `edit_verify`;
    # the confirmation itself is the single-use approval consumed by the route.
    return Actor(id="promoter", operator=True, workspace_id=binding_id, autonomy="edit_verify")


def _recent_workspace(code_store: Any, run_id: str) -> Optional[dict[str, Any]]:
    """The run's most recent unreleased disposable workspace, or None."""
    if code_store is None:
        return None
    try:
        rows = code_store.list_workspaces(run_id=run_id, limit=20)
    except Exception:  # noqa: BLE001 - a store failure means "nothing to promote"
        return None
    live = [r for r in rows if not r.get("releasedAt")]
    return live[0] if live else None


def _checkout_of_workspace(worktree_root: str) -> Optional[Path]:
    """The checkout a scratch workspace was cut from, derived from its path.

    A disposable workspace always lives at
    ``<checkout>/<SCRATCH_RELPATH>/<id>``. Used only for rows written before the
    checkout column existed; a path that does not match the layout returns None
    so the caller can fall back instead of guessing.
    """
    if not worktree_root:
        return None
    try:
        probe = Path(worktree_root).parent  # skip the workspace id segment
        for part in reversed(Path(worktree_mod.SCRATCH_RELPATH).parts):
            if probe.name != part:
                return None
            probe = probe.parent
    except (OSError, ValueError):
        return None
    if not probe.name:
        return None
    return probe


def build_preview(
    run_id: str, *, code_store: Any, checkout_root: Optional[Path] = None,
) -> dict[str, Any]:
    """The exact set of changes a run made, ready for a human to confirm.

    Raises `PromotionError` when there is nothing to promote, the change is
    already in the real folder, or the change touches a protected/escaping path.
    Reads only; writes nothing.
    """
    row = _recent_workspace(code_store, run_id)
    if row is None:
        raise PromotionError(
            "PROMOTION_NO_CHANGE",
            "There's no prepared change from this task to put into your project.",
        )
    strategy = str(row.get("strategy") or "")
    if strategy == "direct":
        raise PromotionError(
            "PROMOTION_ALREADY_DIRECT",
            "This task already changed your folder directly, so there's nothing to put back.",
        )
    ws_id = str(row.get("id") or "")
    worktree_root = Path(str(row.get("root") or ""))
    if not ws_id or not worktree_root.is_dir():
        raise PromotionError(
            "PROMOTION_NO_CHANGE",
            "The prepared change from this task is no longer available.",
        )
    # The PROJECT this build was cut from. An explicit argument wins (tests and
    # callers that already resolved it); otherwise the root recorded when the
    # workspace was allocated; only a legacy row with no root falls back to the
    # currently selected default. Never "whatever is selected now" when the build
    # names its own project — that is how a diff lands in the wrong repository.
    recorded_checkout = str(row.get("checkoutRoot") or "").strip()
    if checkout_root is not None:
        checkout = Path(checkout_root)
    elif recorded_checkout:
        checkout = Path(recorded_checkout)
    else:
        # A row from before the column existed: the scratch path itself names the
        # checkout (<checkout>/.black-eclipse/code-workspaces/<id>). That is still
        # better than the selected root; only an unreadable path falls back.
        checkout = _checkout_of_workspace(str(row.get("root") or "")) or default_root()
    if checkout is None:
        raise PromotionError("PROMOTION_UNAVAILABLE", "There's no project folder to put the change into.")
    checkout = Path(checkout)
    if not checkout.is_dir():
        raise PromotionError(
            "PROMOTION_UNAVAILABLE",
            "The project this build belongs to is no longer at its recorded folder.",
        )
    base = str(row.get("baseRevision") or "HEAD") or "HEAD"

    diff = worktree_mod.diff(ws_id, base, strategy=strategy, root=checkout)
    specs: list[tuple[str, str]] = []
    specs += [(p, "create") for p in (diff.get("added") or [])]
    specs += [(p, "overwrite") for p in (diff.get("modified") or [])]
    specs += [(p, "delete") for p in (diff.get("deleted") or [])]
    if not specs:
        raise PromotionError(
            "PROMOTION_NO_CHANGE",
            "This task didn't change any files, so there's nothing to put into your project.",
        )
    if len(specs) > MAX_PROMOTION_FILES:
        raise PromotionError(
            "PROMOTION_TOO_MANY_FILES",
            f"This change touches more than {MAX_PROMOTION_FILES} files; Eclipse won't put it in at once.",
        )

    files: list[dict[str, Any]] = []
    total_bytes = 0
    for raw_path, op in specs:
        rel = str(raw_path).replace("\\", "/").lstrip("./")
        if not rel:
            continue
        if policy.is_protected(rel) or policy.is_ignored(rel):
            raise PromotionError(
                "PROMOTION_OUT_OF_SCOPE",
                "This change touches a file Eclipse isn't allowed to put into your project.",
            )
        try:
            policy.resolve_within(checkout, rel)
        except policy.PolicyDenial as denial:
            raise PromotionError("PROMOTION_OUT_OF_SCOPE", denial.reason) from denial

        target = checkout / rel
        before_hash: Optional[str] = None
        before_text = ""
        if target.exists():
            before_text = _read_text_strict(target)
            before_hash = _sha_text(before_text)
        after_hash: Optional[str] = None
        after_text: Optional[str] = None
        if op != "delete":
            after_text = _read_text_strict(worktree_root / rel)
            after_hash = _sha_text(after_text)
            total_bytes += len(after_text.encode("utf-8"))

        if op == "create" and before_hash is not None:
            raise PromotionError(
                "PROMOTION_BASE_CHANGED",
                f"{rel} already exists in your project, so Eclipse won't overwrite it.",
            )
        if op in ("overwrite", "delete") and before_hash is None:
            raise PromotionError(
                "PROMOTION_BASE_CHANGED",
                f"{rel} is missing from your project, so Eclipse can't put this change in.",
            )
        hunks = build_hunks(before_text if op != "create" else "", after_text or "")
        files.append({
            "path": rel, "op": op, "beforeHash": before_hash, "afterHash": after_hash,
            "hunks": hunks,
        })

    if total_bytes > MAX_PROMOTION_BYTES:
        raise PromotionError(
            "PROMOTION_TOO_LARGE",
            "This change is too large to put into your project at once.",
        )

    files.sort(key=lambda f: str(f.get("path")))
    added = sum(1 for f in files if f["op"] == "create")
    modified = sum(1 for f in files if f["op"] == "overwrite")
    deleted = sum(1 for f in files if f["op"] == "delete")
    return {
        "workspaceId": ws_id,
        "runId": run_id,
        "checkoutRoot": str(checkout),
        "worktreeRoot": str(worktree_root),
        "strategy": strategy,
        "files": files,
        "filesChanged": len(files),
        "added": added,
        "modified": modified,
        "deleted": deleted,
        "previewDigest": promotion_digest(files),
        "unified": str(diff.get("unified") or ""),
        "truncated": bool(diff.get("truncated")),
    }


_BASE_CODES = {"PATCH_BASE_MISMATCH", "PATCH_FILE_EXISTS", "PATCH_FILE_MISSING"}
_SCOPE_CODES = {
    "PATCH_PROTECTED_PATH", "PATCH_OUTSIDE_SCOPE", "PATCH_PROJECT_DENIED",
    "PATCH_PATH_TRAVERSAL", "PATCH_PATH_ABSOLUTE", "PATCH_PATH_SYMLINK",
}


def _map_patch_error(code: str, reason: str) -> PromotionError:
    if code in _BASE_CODES:
        return PromotionError("PROMOTION_BASE_CHANGED", reason or "Your project changed since the preview.")
    if code in _SCOPE_CODES:
        return PromotionError("PROMOTION_OUT_OF_SCOPE", reason or "This change isn't allowed in your project.")
    if code == "PATCH_TOO_LARGE":
        return PromotionError("PROMOTION_TOO_LARGE", reason or "This change is too large.")
    return PromotionError("PROMOTION_FAILED", reason or code or "The change couldn't be put into your project.")


def filter_preview(
    preview: dict[str, Any], include: Iterable[str],
    *, hunks: Optional[Iterable[str]] = None,
) -> dict[str, Any]:
    """Narrow a preview to the files (and hunks) the reviewer kept.

    `include` names whole files; `hunks` names slices as `"path#hN"`. An empty
    `hunks` means "every hunk of the kept files". A create/delete file cannot be
    split (half a new or deleted file is not a meaningful change). The digest is
    recomputed over exactly the KEPT set, so the approval is bound to what will
    be written.
    """
    keep = {
        str(path).replace("\\", "/").lstrip("./")
        for path in include if str(path or "").strip()
    }
    tokens = {str(token) for token in (hunks or []) if str(token).strip()}
    split = bool(tokens)
    # Empty (or whitespace-only) include with no hunk selection means "keep the
    # whole preview". Returning the SAME object makes that identity explicit:
    # no digest, count, or ordering is recomputed for a no-op selection.
    if not keep and not tokens:
        return preview

    def _selected(item: dict[str, Any]) -> list[str]:
        ids = [str(h.get("id")) for h in (item.get("hunks") or []) if isinstance(h, dict)]
        if not split:
            return ids
        want = {token.split("#", 1)[1] for token in tokens if token.startswith(str(item.get("path")) + "#")}
        return [hunk_id for hunk_id in ids if hunk_id in want]

    files: list[dict[str, Any]] = []
    for item in (preview.get("files") or []):
        path = str(item.get("path") or "")
        if keep and path not in keep:
            continue
        ids = [str(h.get("id")) for h in (item.get("hunks") or []) if isinstance(h, dict)]
        selected = _selected(item)
        if ids and not selected:
            continue
        if item.get("op") in ("create", "delete") and ids and len(selected) != len(ids):
            raise PromotionError(
                "PROMOTION_PARTIAL_FILE",
                "A new or removed file has to be accepted as a whole.",
            )
        entry = dict(item)
        if ids and selected and len(selected) != len(ids):
            entry["selectedHunks"] = selected
            entry["partial"] = True
            target = Path(str(preview.get("checkoutRoot") or "")) / path
            try:
                before_text = _read_text_strict(target)
            except PromotionError:
                raise
            if _sha_text(before_text) != item.get("beforeHash"):
                raise PromotionError(
                    "PROMOTION_BASE_CHANGED",
                    f"{path} changed in your project since the preview.",
                )
            result_text = _apply_hunks(before_text, list(item.get("hunks") or []), set(selected))
            entry["afterHash"] = _sha_text(result_text)
        files.append(entry)
    if not files:
        raise PromotionError(
            "PROMOTION_NO_FILES",
            "None of the selected changes are part of this change.",
        )
    files.sort(key=lambda item: str(item.get("path")))
    return {
        **preview,
        "files": files,
        "filesChanged": len(files),
        "added": sum(1 for item in files if item.get("op") == "create"),
        "modified": sum(1 for item in files if item.get("op") == "overwrite"),
        "deleted": sum(1 for item in files if item.get("op") == "delete"),
        "previewDigest": promotion_digest(files, tokens if split else None),
        "filtered": True,
        "hunksSelected": split,
    }


def apply_promotion(preview: dict[str, Any]) -> dict[str, Any]:
    """Apply a CONFIRMED preview to the real checkout.

    The caller MUST have already consumed the single-use approval bound to
    `preview['previewDigest']`. This function re-checks the previewed content
    (each disposable-copy file still hashes to its previewed value) and the target
    base (each checkout file still hashes to its previewed value), then writes
    atomically through `patch.apply`. Raises `PromotionError` on any mismatch.
    """
    checkout = Path(str(preview.get("checkoutRoot") or ""))
    worktree_root = Path(str(preview.get("worktreeRoot") or ""))
    files = list(preview.get("files") or [])
    if not checkout or not worktree_root or not files:
        raise PromotionError("PROMOTION_NO_CHANGE", "There's nothing to put into your project.")

    # W7: a landed change is undoable too. One pre-promotion snapshot; `enforce`
    # refuses the landing rather than applying an un-undoable change.
    try:
        from .checkpoints import CheckpointError, snapshot  # noqa: PLC0415

        snapshot(
            checkout, reason="before-promote",
            run_id=str(preview.get("runId") or preview.get("taskId") or ""),
            turn_key=str(preview.get("previewDigest") or "promote"),
        )
    except CheckpointError as exc:
        from .checkpoints import checkpoint_mode  # noqa: PLC0415

        if checkpoint_mode() == "enforce":
            raise PromotionError(exc.code, exc.reason) from exc
    except Exception:  # noqa: BLE001 - best-effort in warn/off
        pass

    ops: list[PatchOp] = []
    for f in files:
        rel = str(f.get("path") or "")
        op = str(f.get("op") or "")
        if not rel or op not in ("create", "overwrite", "delete"):
            raise PromotionError("PROMOTION_FAILED", "The prepared change is not valid.")
        if op == "delete":
            ops.append(PatchOp(
                op="delete", path=rel, reason="Put the reviewed change into the project.",
                expect_hash=f.get("beforeHash"),
            ))
            continue
        if f.get("partial"):
            target = checkout / rel
            before_text = _read_text_strict(target)
            if _sha_text(before_text) != f.get("beforeHash"):
                raise PromotionError(
                    "PROMOTION_BASE_CHANGED",
                    "Your project changed since the preview; review the change again.",
                )
            selected = {str(h) for h in (f.get("selectedHunks") or [])}
            text = _apply_hunks(before_text, list(f.get("hunks") or []), selected)
            if _sha_text(text) != f.get("afterHash"):
                raise PromotionError(
                    "PROMOTION_PREVIEW_STALE",
                    "The prepared change was altered after you reviewed it; review it again.",
                )
            ops.append(PatchOp(
                op="overwrite", path=rel, reason="Put the reviewed change into the project.",
                content=text, expect_hash=f.get("beforeHash"),
            ))
            continue
        text = _read_text_strict(worktree_root / rel)
        if _sha_text(text) != f.get("afterHash"):
            raise PromotionError(
                "PROMOTION_PREVIEW_STALE",
                "The prepared change was altered after you reviewed it; review it again.",
            )
        if op == "create":
            ops.append(PatchOp(
                op="create", path=rel, reason="Put the reviewed change into the project.",
                content=text,
            ))
        else:
            ops.append(PatchOp(
                op="overwrite", path=rel, reason="Put the reviewed change into the project.",
                content=text, expect_hash=f.get("beforeHash"),
            ))

    # Capture the ORIGINAL contents BEFORE writing, so the change can be put back
    # exactly (inverse promotion). Reads only; a failure here aborts before any
    # write. Both versions survive: the backup directory is kept after apply.
    rollback_id = _capture_backup(checkout, preview, files)

    binding_id = "promote_" + hashlib.sha256(str(checkout).encode("utf-8")).hexdigest()[:16]
    binding = WorkspaceBinding(id=binding_id, root=checkout, base_revision="", strategy="direct")
    resolved = ResolvedWorkspace(
        id=binding_id, root=checkout, base_revision="", strategy="direct", binding=binding,
    )
    actor = _operator_actor(binding_id)
    try:
        result = patch_mod.apply(resolved, ops, scope=(), binding=binding, actor=actor)
    except PatchError as exc:
        _discard_backup(checkout, rollback_id)
        raise _map_patch_error(exc.code, exc.reason) from exc
    if not result.get("ok"):
        _discard_backup(checkout, rollback_id)
        refused = result.get("refused") or {}
        raise _map_patch_error(str(refused.get("code") or ""), str(refused.get("reason") or ""))

    # Whole-file contents are not echoed back: the caller only needs a summary.
    applied = [
        {k: v for k, v in record.items() if k not in ("old", "new")}
        for record in result.get("applied", [])
    ]
    return {
        "ok": True,
        "checkoutRoot": str(checkout),
        "filesChanged": int(result.get("filesChanged") or 0),
        "totalBytes": int(result.get("totalBytes") or 0),
        "applied": applied,
        "previewDigest": preview.get("previewDigest"),
        # The id to put this exact change back (rollback_promotion).
        "rollbackId": rollback_id,
    }


def _rollback_dir(checkout: Path, rollback_id: str) -> Path:
    return checkout / ROLLBACK_ROOT_RELPATH / rollback_id


def _capture_backup(checkout: Path, preview: dict[str, Any], files: list[dict[str, Any]]) -> str:
    """Save the ORIGINAL contents of every file the promotion will touch.

    Returns the rollback id (the preview digest). Only reads the checkout, so the
    caller can abort before any write if this raises.
    """
    rollback_id = str(preview.get("previewDigest") or "")
    if not rollback_id:
        raise PromotionError("PROMOTION_FAILED", "The prepared change is missing its identity.")
    directory = _rollback_dir(checkout, rollback_id)
    directory.mkdir(parents=True, exist_ok=True)
    originals: list[dict[str, Any]] = []
    for f in files:
        rel = str(f.get("path") or "")
        op = str(f.get("op") or "")
        entry: dict[str, Any] = {
            "path": rel, "op": op, "existed": op != "create",
            "beforeHash": f.get("beforeHash"), "afterHash": f.get("afterHash"),
        }
        if op != "create":
            text = _read_text_strict(checkout / rel)
            backup_name = rel.replace("/", "__")
            (directory / backup_name).write_text(text, encoding="utf-8")
            entry["backup"] = backup_name
        originals.append(entry)
    manifest = {
        "rollbackId": rollback_id,
        "checkoutRoot": str(checkout),
        "previewDigest": preview.get("previewDigest"),
        "workspaceId": preview.get("workspaceId"),
        "runId": preview.get("runId"),
        "files": originals,
    }
    (directory / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return rollback_id


def _discard_backup(checkout: Path, rollback_id: str) -> None:
    try:
        shutil.rmtree(_rollback_dir(checkout, rollback_id))
    except OSError:
        pass


def rollback_promotion(rollback_id: str, *, checkout_root: Optional[Path] = None) -> dict[str, Any]:
    """Put a previously-promoted change back — the exact original contents.

    The inverse of `apply_promotion`: a file the promotion CREATED is removed, a
    file it EDITED is restored from the saved backup, and a file it DELETED is
    recreated. Both versions survive (the backup is kept, never discarded).
    Reuses `code/patch.py` (path confinement, protected paths, atomicity) and
    refuses if a restored file changed since the promotion (`PROMOTION_BASE_CHANGED`).
    """
    checkout = Path(checkout_root) if checkout_root is not None else default_root()
    if checkout is None:
        raise PromotionError("PROMOTION_UNAVAILABLE", "There's no project folder to put the change back into.")
    checkout = Path(checkout)
    directory = _rollback_dir(checkout, rollback_id)
    manifest_path = directory / "manifest.json"
    if not manifest_path.is_file():
        raise PromotionError("PROMOTION_NO_ROLLBACK", "There's no saved version to put back for this change.")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise PromotionError("PROMOTION_NO_ROLLBACK", "The saved version is unreadable.") from exc
    recorded = str(manifest.get("checkoutRoot") or "")
    if recorded and Path(os.path.realpath(recorded)) != Path(os.path.realpath(checkout)):
        raise PromotionError("PROMOTION_OUT_OF_SCOPE", "The saved version belongs to a different project folder.")

    ops: list[PatchOp] = []
    restored: list[str] = []
    for f in manifest.get("files") or []:
        rel = str(f.get("path") or "")
        op = str(f.get("op") or "")
        if not rel:
            continue
        if policy.is_protected(rel) or policy.is_ignored(rel):
            raise PromotionError("PROMOTION_OUT_OF_SCOPE", "This change touches a file Eclipse isn't allowed to change.")
        if op == "create":
            # The promotion created the file; putting it back removes it.
            ops.append(PatchOp(op="delete", path=rel, reason="Put the previous version back."))
        elif op == "delete":
            text = (directory / str(f.get("backup"))).read_text(encoding="utf-8")
            ops.append(PatchOp(op="create", path=rel, reason="Put the previous version back.", content=text))
        else:  # overwrite — restore the saved original, refusing if the file moved on
            text = (directory / str(f.get("backup"))).read_text(encoding="utf-8")
            ops.append(PatchOp(op="overwrite", path=rel, reason="Put the previous version back.",
                               content=text, expect_hash=f.get("afterHash")))
        restored.append(rel)

    if not ops:
        raise PromotionError("PROMOTION_NO_ROLLBACK", "There's nothing to put back for this change.")

    binding_id = "rollback_" + hashlib.sha256(str(checkout).encode("utf-8")).hexdigest()[:16]
    binding = WorkspaceBinding(id=binding_id, root=checkout, base_revision="", strategy="direct")
    resolved = ResolvedWorkspace(
        id=binding_id, root=checkout, base_revision="", strategy="direct", binding=binding,
    )
    actor = _operator_actor(binding_id)
    try:
        result = patch_mod.apply(resolved, ops, scope=(), binding=binding, actor=actor)
    except PatchError as exc:
        raise _map_patch_error(exc.code, exc.reason) from exc
    if not result.get("ok"):
        refused = result.get("refused") or {}
        raise _map_patch_error(str(refused.get("code") or ""), str(refused.get("reason") or ""))
    return {
        "ok": True,
        "rollbackId": rollback_id,
        "restored": sorted(restored),
        "filesChanged": int(result.get("filesChanged") or 0),
    }


def public_preview(preview: dict[str, Any]) -> dict[str, Any]:
    """The client-safe preview: no worktree root, no full hunk text.

    Hunk IDs, headers and a bounded changed-line preview are enough for the UI
    to offer per-hunk choices; `beforeText`/`afterText` stay server-side and are
    re-derived at apply time.
    """
    view = {k: v for k, v in preview.items() if k != "worktreeRoot"}
    files: list[dict[str, Any]] = []
    for item in view.get("files") or []:
        entry = dict(item)
        entry["hunks"] = [
            {k: h.get(k) for k in (
                "id", "header", "beforeStart", "beforeLines", "afterStart",
                "afterLines", "preview",
            )}
            for h in (item.get("hunks") or [])
        ]
        files.append(entry)
    view["files"] = files
    return view


__all__ = [
    "MAX_PROMOTION_FILES",
    "PROMOTION_KIND",
    "ROLLBACK_ROOT_RELPATH",
    "PromotionError",
    "apply_promotion",
    "build_preview",
    "promotion_digest",
    "public_preview",
    "rollback_promotion",
]
