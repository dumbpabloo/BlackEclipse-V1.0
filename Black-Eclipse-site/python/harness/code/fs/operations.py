"""Basic filesystem operations â€” list, read, write, create, delete, move, copy,
rename, mkdir, rmdir.

Every operation:
  1. authorizes the capability through `permissions.require` (centralized);
  2. resolves the path through `resolver.resolve` (centralized confinement);
  3. honours `dry_run` by computing the change but writing nothing;
  4. for a mutation, backs up the target in a `FileTransaction` and rolls back on
     any failure, so a partial change never survives;
  5. re-checks staleness when `expected_hash` is supplied (optimistic concurrency);
  6. returns a structured record with the diff where useful.

Nothing here calls `realpath` â€” `resolver` and `workspace` own that.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any, Optional

from .. import policy
from .. import project_policy
from . import encoding as enc
from . import errors as err
from . import metadata as meta
from .. import media as media_mod
from . import resolver as res
from .context import FsContext
from .diff import preview_for
from .transactions import FileTransaction

MAX_LIST_ENTRIES = 5_000
MAX_READ_LINES = 2_000
MAX_DEPTH = 12


# â”€â”€ helpers â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€


def _current_hash(path: Path) -> Optional[str]:
    if not path.exists() or path.is_dir():
        return None
    return meta.hash_file(path)


def _check_hash(path: Path, expected: Optional[str], ws: Any) -> None:
    if expected is None:
        return
    actual = _current_hash(path)
    if actual != expected:
        raise err.FsError(
            err.STALE_FILE,
            "That file changed since it was read.",
            path=res.relative(ws, path),
            details={"expectedHash": expected, "actualHash": actual},
        )


def _refuse_binary(path: Path, ws: Any) -> None:
    if res.is_binary_file(path):
        raise err.FsError(
            err.UNSUPPORTED_BINARY_OPERATION,
            "That file is not editable text.",
            path=res.relative(ws, path),
        )


def _entry(path: Path, ws: Any, *, include_sizes: bool) -> dict[str, Any]:
    rel = res.relative(ws, path)
    is_dir = path.is_dir()
    entry: dict[str, Any] = {
        "path": rel + ("/" if is_dir else ""),
        "type": "dir" if is_dir else "file",
        "name": path.name,
        "hidden": path.name.startswith("."),
        "protected": policy.is_protected(rel),
        "ignored": policy.is_ignored(rel),
    }
    if not is_dir:
        try:
            st = path.stat()
            entry["size"] = st.st_size
            entry["extension"] = path.suffix
            entry["modified"] = meta._iso(st.st_mtime)
            if include_sizes:
                entry["mimeType"] = meta.mime_type(path)
        except OSError:
            pass
    return entry


# â”€â”€ list_directory â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€


def list_directory(
    ctx: FsContext,
    path: str = ".",
    *,
    recursive: bool = False,
    max_depth: int = 1,
    limit: int = 500,
    offset: int = 0,
    include_hidden: bool = False,
    include_ignored: bool = False,
    pattern: Optional[str] = None,
    directories_only: bool = False,
    files_only: bool = False,
    sort: str = "name",
    include_sizes: bool = False,
) -> dict[str, Any]:
    ctx.require("read", "")
    start = res.require_directory(ctx.ws, path or ".")
    depth = max(0, min(int(max_depth), MAX_DEPTH))
    bound = max(1, min(int(limit), MAX_LIST_ENTRIES))
    root_depth = len(start.relative_to(ctx.ws.root).parts)
    entries: list[dict[str, Any]] = []
    truncated = False

    def _allowed(p: Path) -> bool:
        rel = res.relative(ctx.ws, p)
        if policy.is_protected(rel) or policy.is_ignored(rel):
            return include_ignored and not policy.is_protected(rel)
        return True

    def _collect(current: Path) -> None:
        nonlocal truncated
        try:
            children = sorted(current.iterdir(), key=lambda p: (not p.is_dir(), p.name.casefold()))
        except OSError:
            return
        for child in children:
            if child.name.startswith(".") and not include_hidden:
                continue
            if not _allowed(child):
                continue
            if files_only and child.is_dir():
                continue
            if directories_only and not child.is_dir():
                continue
            if pattern and not child.is_dir():
                import fnmatch

                if not fnmatch.fnmatch(child.name, pattern):
                    continue
            entries.append(_entry(child, ctx.ws, include_sizes=include_sizes))
            if len(entries) >= MAX_LIST_ENTRIES:
                truncated = True
                return

    if recursive:
        for dirpath, dirnames, _filenames in os.walk(start, followlinks=False):
            current = Path(dirpath)
            if len(current.relative_to(ctx.ws.root).parts) - root_depth >= depth:
                dirnames[:] = []
            dirnames[:] = [
                d for d in dirnames
                if (include_hidden or not d.startswith(".")) and _allowed(current / d)
            ]
            _collect(current)
            if truncated:
                break
    else:
        _collect(start)

    if sort == "modified":
        entries.sort(key=lambda e: e.get("modified") or "", reverse=True)
    elif sort == "size":
        entries.sort(key=lambda e: e.get("size") or 0, reverse=True)
    total = len(entries)
    page = entries[offset: offset + bound]
    if offset + len(page) < total:
        truncated = True
    return {
        "status": "ok",
        "path": res.relative(ctx.ws, start),
        "entries": page,
        "count": len(page),
        "totalCount": total,
        "offset": offset,
        "truncated": truncated,
        "recursive": recursive,
    }


# â”€â”€ read_file â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€


def read_media(ctx: FsContext, path: str) -> Optional[dict[str, Any]]:
    """Read a recognised image/PDF as bounded media, or None for other files.

    Uses the same confinement + read permission as `read_file`; a media file is
    NOT decoded as text. Oversized media raises `TOO_LARGE`; a binary that is
    not a recognised media type returns None so the normal read refuses it.
    """
    ctx.require("read", path)
    target = res.require_regular_file(ctx.ws, path, allow_ignored=True)
    try:
        size = target.stat().st_size
    except OSError:
        return None
    if size > media_mod.MAX_MEDIA_BYTES:
        raise err.FsError(
            err.TOO_LARGE,
            f"That media file is larger than {media_mod.MAX_MEDIA_BYTES // (1024 * 1024)} MB.",
            path=res.relative(ctx.ws, target),
        )
    try:
        data = target.read_bytes()
    except OSError:
        return None
    sniffed = media_mod.sniff_mime(data)
    if sniffed is None:
        return None
    mime, kind = sniffed
    rel = res.relative(ctx.ws, target)
    return {
        "status": "ok",
        "path": rel,
        "media": media_mod.media_payload(rel, data, mime, kind),
    }


def read_file(
    ctx: FsContext,
    path: str,
    *,
    start_line: Optional[int] = None,
    end_line: Optional[int] = None,
    offset: Optional[int] = None,
    limit: Optional[int] = None,
    encoding: Optional[str] = None,
    line_numbers: bool = True,
) -> dict[str, Any]:
    ctx.require("read", path)
    target = res.require_regular_file(ctx.ws, path)
    if res.is_binary_file(target):
        raise err.FsError(err.BINARY_FILE, "That file is not readable text.", path=res.relative(ctx.ws, target))
    decoded = enc.read_text(target, encoding=encoding)
    all_lines = decoded.text.splitlines()
    total_lines = len(all_lines)
    start = max(0, (start_line - 1) if start_line else (offset or 0))
    if end_line is not None:
        end = min(total_lines, max(start, end_line))
    elif limit is not None:
        end = min(total_lines, start + max(0, int(limit)))
    else:
        end = min(total_lines, start + MAX_READ_LINES)
    window = all_lines[start:end]
    continues = end < total_lines
    numbered = [f"{start + i + 1:>6}\t{text}" for i, text in enumerate(window)]
    return {
        "status": "ok",
        "path": res.relative(ctx.ws, target),
        "startLine": start + 1,
        "endLine": start + len(window),
        "totalLines": total_lines,
        "nextStartLine": (end + 1) if continues else None,
        "lines": window,
        "content": numbered if line_numbers else window,
        "encoding": decoded.encoding,
        "bom": decoded.had_bom,
        "newline": decoded.newline,
        "sha256": meta.hash_file(target),
    }


# â”€â”€ write_file / create_file â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€


def _apply_write(
    ctx: FsContext, target: Path, text: str, *, encoding: str, had_bom: bool,
    txn: FileTransaction,
) -> int:
    txn.backup(target)
    return enc.write_text_atomic(target, text, encoding=encoding, had_bom=had_bom)


def _finish_write(
    ctx: FsContext, target: Path, rel: str, *,
    before: Optional[str], written: str, nbytes: int, created: bool,
) -> dict[str, Any]:
    """Post-write format + verify + diagnostics, shared by write and create.

    Gap #5: the project's formatter runs before the hash and diff are reported,
    so the result describes the bytes on disk. Every step is bounded and
    non-fatal; a formatter failure leaves the written text exactly as written.
    """
    from .. import formatting as fmt_mod  # noqa: PLC0415
    from .. import post_edit  # noqa: PLC0415
    from . import verify as verify_mod  # noqa: PLC0415

    format_info = fmt_mod.maybe_format(target, rel)
    final_text = written
    if format_info.get("changed"):
        try:
            final_text = enc.read_text(target).text
        except err.FsError:  # pragma: no cover - the write just succeeded
            final_text = written
    after_hash = _current_hash(target)
    result: dict[str, Any] = {
        "status": "ok",
        "path": rel,
        "written": True,
        "bytesWritten": nbytes,
        "created": created,
        "sha256": after_hash,
        **preview_for(rel, before, final_text),
        "format": format_info,
    }
    result["verification"] = verify_mod.verify_mutation(
        ctx.ws, rel, expected_hash=after_hash,
        expect_text=None if format_info.get("changed") else (written or ""),
    )
    result["diagnostics"] = post_edit.diagnostics_for(ctx.ws, rel, final_text)
    return result


def write_file(
    ctx: FsContext,
    path: str,
    content: str,
    *,
    encoding: Optional[str] = None,
    create_if_missing: bool = False,
    overwrite: bool = True,
    expected_hash: Optional[str] = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    ctx.require("write", path)
    target = res.resolve(ctx.ws, path, for_write=True)
    rel = res.relative(ctx.ws, target)
    exists = target.exists()
    if target.is_dir():
        raise err.FsError(err.NOT_A_FILE, "That is a folder, not a file.", path=rel)
    if not exists and not create_if_missing and not overwrite:
        raise err.FsError(err.PATH_NOT_FOUND, "That file isn't there.", path=rel)
    if not exists and not create_if_missing:
        # Default: writing a missing file is a refusal unless create is asked for.
        raise err.FsError(err.PATH_NOT_FOUND, "That file isn't there. Use create_file instead.", path=rel)
    if exists and not overwrite:
        raise err.FsError(err.FILE_EXISTS, "That file already exists and overwrite is off.", path=rel)
    if exists:
        _refuse_binary(target, ctx.ws)
        _check_hash(target, expected_hash, ctx.ws)
    used_encoding = "utf-8"
    had_bom = False
    before: Optional[str] = None
    if exists:
        decoded = enc.read_text(target, encoding=encoding)
        used_encoding, had_bom = decoded.encoding, decoded.had_bom
        before = decoded.text
    if dry_run or ctx.dry_run:
        preview = preview_for(rel, before, content)
        return {"status": "ok", "dryRun": True, "path": rel, "written": False, **preview}
    txn = FileTransaction(ctx.ws)
    nbytes = _apply_write(ctx, target, content, encoding=used_encoding, had_bom=had_bom, txn=txn)
    txn.commit()
    return _finish_write(
        ctx, target, rel, before=before, written=content,
        nbytes=nbytes, created=not exists,
    )


def create_file(
    ctx: FsContext,
    path: str,
    content: str = "",
    *,
    overwrite: bool = False,
    encoding: Optional[str] = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    ctx.require("create", path)
    target = res.resolve(ctx.ws, path, for_write=True)
    rel = res.relative(ctx.ws, target)
    if target.exists():
        if target.is_dir():
            raise err.FsError(err.DIRECTORY_EXISTS, "A folder already has that name.", path=rel)
        if not overwrite:
            raise err.FsError(err.FILE_EXISTS, "That file already exists.", path=rel)
        _refuse_binary(target, ctx.ws)
    if dry_run or ctx.dry_run:
        before = None
        if target.exists():
            before = enc.read_text(target).text
        return {"status": "ok", "dryRun": True, "path": rel, "created": False, **preview_for(rel, before, content or "")}
    txn = FileTransaction(ctx.ws)
    nbytes = _apply_write(ctx, target, content or "", encoding=encoding or "utf-8", had_bom=False, txn=txn)
    txn.commit()
    return _finish_write(
        ctx, target, rel, before=None, written=content or "",
        nbytes=nbytes, created=True,
    )


# â”€â”€ delete_file / delete_directory â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€


def delete_file(
    ctx: FsContext,
    path: str,
    *,
    expected_hash: Optional[str] = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    ctx.require("delete", path)
    target = res.resolve(ctx.ws, path, for_change=True)
    rel = res.relative(ctx.ws, target)
    if target.is_dir():
        raise err.FsError(err.NOT_A_FILE, "Use delete_directory for a folder.", path=rel)
    if not target.exists():
        raise err.FsError(err.PATH_NOT_FOUND, "That file isn't there.", path=rel)
    _check_hash(target, expected_hash, ctx.ws)
    before = None
    if not res.is_binary_file(target):
        before = enc.read_text(target).text
    if dry_run or ctx.dry_run:
        return {"status": "ok", "dryRun": True, "path": rel, "deleted": False, **preview_for(rel, before, None)}
    txn = FileTransaction(ctx.ws)
    txn.backup(target)
    try:
        target.unlink()
    except OSError as exc:
        txn.rollback()
        raise err.FsError(err.PERMISSION_DENIED, "That file couldn't be removed.", path=rel) from exc
    txn.commit()
    return {"status": "ok", "path": rel, "deleted": True, **preview_for(rel, before, None)}


def delete_directory(
    ctx: FsContext,
    path: str,
    *,
    recursive: bool = False,
    dry_run: bool = False,
) -> dict[str, Any]:
    ctx.require("delete", path)
    target = res.resolve(ctx.ws, path, for_change=True)
    rel = res.relative(ctx.ws, target)
    if not target.exists():
        raise err.FsError(err.PATH_NOT_FOUND, "That folder isn't there.", path=rel)
    if not target.is_dir():
        raise err.FsError(err.NOT_A_DIRECTORY, "That isn't a folder.", path=rel)
    if target == ctx.ws.root:
        raise err.FsError(err.PERMISSION_DENIED, "The project folder itself can't be removed.", path=".")
    children = list(target.iterdir()) if target.is_dir() else []
    if children and not recursive:
        raise err.FsError(
            err.PERMISSION_DENIED,
            "That folder isn't empty. Recursive removal must be asked for explicitly.",
            path=rel,
            details={"entries": len(children)},
        )
    # Every path under the folder must be allowed before anything is removed.
    protected = [
        res.relative(ctx.ws, p) for p in target.rglob("*")
        if policy.is_protected(res.relative(ctx.ws, p))
        or project_policy.is_denied(ctx.ws.root, res.relative(ctx.ws, p))
    ]
    if protected:
        raise err.FsError(
            err.PERMISSION_DENIED,
            "That folder holds files Eclipse must never remove.",
            path=rel,
            details={"protected": protected[:10]},
        )
    if dry_run or ctx.dry_run:
        return {
            "status": "ok", "dryRun": True, "path": rel, "deleted": False,
            "entries": len(children), "recursive": recursive,
        }
    txn = FileTransaction(ctx.ws)
    txn.backup(target)
    try:
        shutil.rmtree(target)
    except OSError as exc:
        txn.rollback()
        raise err.FsError(err.PERMISSION_DENIED, "That folder couldn't be removed.", path=rel) from exc
    txn.commit()
    return {"status": "ok", "path": rel, "deleted": True, "entries": len(children), "recursive": recursive}


# â”€â”€ create_directory â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€


def create_directory(
    ctx: FsContext,
    path: str,
    *,
    parents: bool = True,
    exist_ok: bool = False,
    dry_run: bool = False,
) -> dict[str, Any]:
    ctx.require("create", path)
    target = res.resolve(ctx.ws, path, for_write=True)
    rel = res.relative(ctx.ws, target)
    if target.exists():
        if target.is_dir():
            if exist_ok:
                return {"status": "ok", "path": rel, "created": False, "alreadyExisted": True}
            raise err.FsError(err.DIRECTORY_EXISTS, "That folder already exists.", path=rel)
        raise err.FsError(err.FILE_EXISTS, "A file already has that name.", path=rel)
    if dry_run or ctx.dry_run:
        return {"status": "ok", "dryRun": True, "path": rel, "created": False}
    try:
        target.mkdir(parents=parents, exist_ok=exist_ok)
    except FileExistsError:
        raise err.FsError(err.FILE_EXISTS, "A file already has that name.", path=rel) from None
    except OSError as exc:
        raise err.FsError(err.PERMISSION_DENIED, "That folder couldn't be created.", path=rel) from exc
    return {"status": "ok", "path": rel, "created": True}


# â”€â”€ move_file / rename_file / copy_file â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€


def _guard_destination(ctx: FsContext, source: Path, destination: Path, *, overwrite: bool) -> str:
    """Check the destination is allowed and not a protected/illegal target."""
    dest_rel = res.relative(ctx.ws, destination)
    if policy.is_protected(dest_rel):
        raise err.FsError(err.PERMISSION_DENIED, "That destination is protected.", path=dest_rel)
    if destination.exists() and not overwrite:
        if destination.is_dir():
            raise err.FsError(err.DIRECTORY_EXISTS, "A folder is already there.", path=dest_rel)
        raise err.FsError(err.FILE_EXISTS, "A file is already there.", path=dest_rel)
    if destination.exists() and destination.is_dir() and not source.is_dir():
        raise err.FsError(err.FILE_EXISTS, "A folder is already there.", path=dest_rel)
    try:
        destination.relative_to(source)
        if source.is_dir():
            raise err.FsError(err.INVALID_PATH, "A folder can't be moved inside itself.", path=dest_rel)
    except ValueError:
        pass
    return dest_rel


def move_file(
    ctx: FsContext,
    source: str,
    destination: str,
    *,
    overwrite: bool = False,
    expected_hash: Optional[str] = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    ctx.require("move", source)
    ctx.require("move", destination)
    src = res.resolve(ctx.ws, source, for_change=True)
    if not src.exists():
        raise err.FsError(err.PATH_NOT_FOUND, "The source isn't there.", path=source)
    _check_hash(src, expected_hash, ctx.ws)
    dst = res.resolve(ctx.ws, destination, for_write=True)
    src_rel, dest_rel = res.relative(ctx.ws, src), _guard_destination(ctx, src, dst, overwrite=overwrite)
    if dry_run or ctx.dry_run:
        return {"status": "ok", "dryRun": True, "moved": False, "from": src_rel, "to": dest_rel}
    txn = FileTransaction(ctx.ws)
    txn.backup(dst)
    txn.backup(src)
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        if dst.exists():
            if dst.is_dir():
                shutil.rmtree(dst)
            else:
                dst.unlink()
        os.replace(src, dst)
    except OSError as exc:
        txn.rollback()
        raise err.FsError(err.PERMISSION_DENIED, "That couldn't be moved.", path=src_rel) from exc
    txn.commit()
    return {"status": "ok", "moved": True, "from": src_rel, "to": dest_rel, "isDirectory": dst.is_dir()}


def rename_file(
    ctx: FsContext,
    path: str,
    new_name: str,
    *,
    overwrite: bool = False,
    dry_run: bool = False,
) -> dict[str, Any]:
    if not new_name or "/" in new_name or "\\" in new_name or new_name in (".", ".."):
        raise err.FsError(err.INVALID_PATH, "A new name can't contain a folder separator.", path=path)
    src = res.resolve(ctx.ws, path)
    destination = str(Path(res.relative(ctx.ws, src)).parent / new_name)
    if destination.startswith("."):
        destination = destination[2:]
    return move_file(ctx, path, destination, overwrite=overwrite, dry_run=dry_run)


def copy_file(
    ctx: FsContext,
    source: str,
    destination: str,
    *,
    overwrite: bool = False,
    recursive: bool = True,
    dry_run: bool = False,
) -> dict[str, Any]:
    ctx.require("copy", source)
    ctx.require("copy", destination)
    src = res.resolve(ctx.ws, source)
    if not src.exists():
        raise err.FsError(err.PATH_NOT_FOUND, "The source isn't there.", path=source)
    if src.is_dir() and not recursive:
        raise err.FsError(err.PERMISSION_DENIED, "Copying a folder needs recursive copy.", path=source)
    dst = res.resolve(ctx.ws, destination, for_write=True)
    src_rel, dest_rel = res.relative(ctx.ws, src), _guard_destination(ctx, src, dst, overwrite=overwrite)
    if dry_run or ctx.dry_run:
        return {"status": "ok", "dryRun": True, "copied": False, "from": src_rel, "to": dest_rel}
    txn = FileTransaction(ctx.ws)
    txn.backup(dst)
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        if dst.exists():
            if dst.is_dir():
                shutil.rmtree(dst)
            else:
                dst.unlink()
        if src.is_dir():
            _copy_tree(ctx, src, dst)
        else:
            shutil.copy2(src, dst)
    except err.FsError:
        txn.rollback()
        raise
    except OSError as exc:
        txn.rollback()
        raise err.FsError(err.PERMISSION_DENIED, "That couldn't be copied.", path=src_rel) from exc
    txn.commit()
    return {"status": "ok", "copied": True, "from": src_rel, "to": dest_rel, "isDirectory": dst.is_dir()}


def _copy_tree(ctx: FsContext, src: Path, dst: Path) -> None:
    for dirpath, dirnames, filenames in os.walk(src, followlinks=False):
        current = Path(dirpath)
        dirnames[:] = [
            d for d in dirnames
            if not policy.is_ignored(res.relative(ctx.ws, current / d))
            and not policy.is_protected(res.relative(ctx.ws, current / d))
        ]
        target_dir = dst / current.relative_to(src)
        target_dir.mkdir(parents=True, exist_ok=True)
        for name in filenames:
            file_rel = res.relative(ctx.ws, current / name)
            if policy.is_ignored(file_rel):
                continue
            if policy.is_protected(file_rel):
                raise err.FsError(err.PERMISSION_DENIED, "A protected file was in the way.", path=file_rel)
            shutil.copy2(current / name, target_dir / name)


__all__ = [
    "MAX_DEPTH",
    "MAX_LIST_ENTRIES",
    "MAX_READ_LINES",
    "copy_file",
    "create_directory",
    "create_file",
    "delete_directory",
    "delete_file",
    "list_directory",
    "move_file",
    "read_file",
    "rename_file",
    "write_file",
]

