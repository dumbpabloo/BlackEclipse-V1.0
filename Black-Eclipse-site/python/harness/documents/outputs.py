"""Where Eclipse puts a file for the person — and how it finds it again.

Two jobs, deliberately in one small module:

  * `publish()` writes a produced file into a REAL, user-visible folder and
    returns its ABSOLUTE path. An answer that says "I made the PDF" is only
    useful when it can add "at C:\\\\Users\\\\you\\\\Documents\\\\Eclipse\\\\x.pdf" — the
    Artifact Center id alone cannot be opened from Explorer.
  * `locate()` searches the folders a person actually uses (Downloads,
    Documents, Desktop, the Eclipse export folder) plus the bound workspace and
    answers with absolute paths, newest first. This is the "find the file you
    just made" path; it never guesses and never invents a result.

Why not reuse the artifact store? The store is content-addressed and lives under
`.black-eclipse/artifacts/` — perfect as a catalog, useless as an address a
person can open. The export folder is the human-facing copy; the artifact entry
is the durable catalog entry. Both are written from the same bytes.

Safety: the export folder is the ONLY place this module writes, file names are
stripped of separators and control characters, and a write is atomic (temp file
+ `os.replace`). Reads are bounded by depth, an entry budget and a skip list, so
a search never turns into a full-disk walk.
"""

from __future__ import annotations

import fnmatch
import os
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Iterable, Optional

#: Set to override the export folder (tests and operators use it).
OUTPUT_DIR_ENV = "ECLIPSE_OUTPUT_DIR"
#: The folder's name under Documents; one stable name people can remember.
OUTPUT_DIR_NAME = "Eclipse"

#: Directories never worth walking, and often huge. Matched case-insensitively.
_SKIP_DIRS = {
    "node_modules", ".git", ".next", ".next-desktop", ".next-dev", "__pycache__",
    "appdata", "library", "system volume information", "$recycle.bin", "venv",
    ".venv", "dist", "target", "site-packages", ".cache", ".gradle", ".m2",
}

#: Stop walking after this many entries — a search must stay bounded.
_MAX_ENTRIES = 60_000


# ── known folders ────────────────────────────────────────────────────────────
# Windows real names come from the shell, not from a guess: a person who moved
# Documents to OneDrive (or to another drive) still gets the right folder.
_WINDOWS_FOLDER_IDS = {
    "documents": "FDD39AD0-238F-46AF-ADB4-6C85480369C7",
    "downloads": "374DE290-123F-4565-9164-39C4925E467B",
    "desktop": "B4BFCC3A-DB2C-424C-B029-7FE99A87C641",
    "home": "5E6C858F-0E22-4760-9AFE-EA3317B67173",
}


def _windows_known_folder(kind: str) -> Optional[Path]:
    folder_id = _WINDOWS_FOLDER_IDS.get(kind)
    if not folder_id or os.name != "nt":
        return None
    try:  # noqa: SIM105 - a missing shell API simply means "no answer"
        import ctypes  # noqa: PLC0415

        class _Guid(ctypes.Structure):
            _fields_ = [("raw", ctypes.c_ubyte * 16)]

        guid = _Guid()
        ctypes.memmove(guid.raw, uuid.UUID(folder_id).bytes_le, 16)
        out = ctypes.c_wchar_p()
        result = ctypes.windll.shell32.SHGetKnownFolderPath(
            ctypes.byref(guid), 0, None, ctypes.byref(out)
        )
        if result != 0 or not out.value:
            return None
        try:
            candidate = Path(out.value)
        finally:
            ctypes.windll.ole32.CoTaskMemFree(out)
        return candidate if candidate.is_dir() else None
    except Exception:  # noqa: BLE001 - fall back to the conventional folder
        return None


def _fallback_folder(kind: str) -> Path:
    home = Path.home()
    if kind == "home":
        return home
    name = {"documents": "Documents", "downloads": "Downloads", "desktop": "Desktop"}[kind]
    for candidate in (home / name, home / "OneDrive" / name, home / "OneDrive"):
        if candidate.is_dir():
            return candidate
    return home


def known_folder(kind: str) -> Path:
    """Documents / Downloads / Desktop / home — the real one when the OS knows it."""
    found = _windows_known_folder(kind)
    if found is not None:
        return found
    return _fallback_folder(kind)


def exports_dir() -> Path:
    """The one folder Eclipse writes user-facing files to, created on demand.

    `<Documents>/Eclipse` by default (the shell-resolved Documents folder, so a
    OneDrive-redirected profile works); `ECLIPSE_OUTPUT_DIR` overrides it.
    """
    override = os.environ.get(OUTPUT_DIR_ENV)
    if override:
        folder = Path(override).expanduser()
    else:
        folder = known_folder("documents") / OUTPUT_DIR_NAME
    folder.mkdir(parents=True, exist_ok=True)
    return folder


# ── writing ──────────────────────────────────────────────────────────────────

def safe_filename(name: str, fallback: str = "eclipse-output") -> str:
    """A file name with no directory part and no control characters."""
    cleaned = "".join(
        ch for ch in str(name or "").replace("\\", "-").replace("/", "-")
        if ch >= " " and ch not in ':<>"|?*'
    ).strip(" .")
    # A name can never carry a traversal segment, even after separator folding.
    cleaned = cleaned.replace("..", "-").strip(" .-")
    return (cleaned or fallback)[:180]


def _unique_target(folder: Path, filename: str) -> Path:
    target = folder / filename
    if not target.exists():
        return target
    stem, suffix = target.stem, target.suffix
    for index in range(2, 1000):
        candidate = folder / f"{stem} ({index}){suffix}"
        if not candidate.exists():
            return candidate
    return folder / f"{stem}-{int(time.time())}{suffix}"


def publish(
    data: bytes, filename: str, *, folder: Optional[Path] = None
) -> Path:
    """Write `data` to the export folder and return the ABSOLUTE path.

    Raises `OSError` when the folder cannot be written; callers report that
    honestly instead of claiming a file exists.
    """
    base = folder or exports_dir()
    base.mkdir(parents=True, exist_ok=True)
    target = _unique_target(base, safe_filename(filename))
    handle = tempfile.NamedTemporaryFile("wb", dir=base, delete=False, suffix=".part")
    try:
        with handle as tmp:
            tmp.write(data)
            tmp_path = Path(tmp.name)
        os.replace(tmp_path, target)
    except BaseException:
        try:
            Path(handle.name).unlink()
        except OSError:
            pass
        raise
    return target.resolve()


def destination(kind: Optional[str] = None) -> Path:
    """Resolve a requested destination: documents (default), downloads, desktop.

    An absolute folder path is accepted too, because "save it on my Desktop" has
    to mean the real Desktop. A relative path is refused — it would depend on a
    working directory the person never chose.
    """
    key = str(kind or "documents").strip().lower()
    if key == "documents":
        # Honors ECLIPSE_OUTPUT_DIR, so the env override and the default folder
        # can never disagree (tests run hermetically through this path).
        return exports_dir()
    if key in ("downloads", "desktop"):
        return known_folder(key) / OUTPUT_DIR_NAME
    folder = Path(key).expanduser()
    if not folder.is_absolute():
        raise ValueError(f"not an absolute folder: {kind!r}")
    return folder


def publish_copy(
    data: bytes, filename: str, save_to: Optional[str] = None
) -> tuple[Optional[str], str]:
    """Publish and return `(absolute_path_or_None, sentence_to_append)`.

    A failed export copy must NOT fail the tool: the Artifact Center copy is
    already durable, so the suffix says plainly where the readable copy went —
    or that it could not be written and why.
    """
    try:
        folder = destination(save_to)
        target = publish(data, filename, folder=folder)
    except (OSError, ValueError) as exc:  # noqa: BLE001 - the artifact copy exists
        return None, f" I could not also save a copy to the requested folder ({exc})."
    return str(target), f" Saved at {target}."


# ── finding ──────────────────────────────────────────────────────────────────

def search_roots() -> list[Path]:
    """The folders a person expects a provided file to land in (deduplicated)."""
    candidates = [
        exports_dir(),
        known_folder("downloads"),
        known_folder("documents"),
        known_folder("desktop"),
    ]
    roots: list[Path] = []
    seen: set[Path] = set()
    # Shallowest first, so a nested folder (Documents/Eclipse under Documents) is
    # the one that gets dropped rather than searched twice.
    ordered = sorted(
        candidates, key=lambda path: len(path.parts) if str(path) else 0
    )
    for candidate in ordered:
        try:
            real = candidate.resolve()
        except OSError:
            continue
        if not candidate.is_dir():
            continue
        # A folder already covered by an earlier, shallower root would only
        # produce duplicate hits (Documents/Eclipse sits under Documents).
        if any(real == other or other in real.parents for other in seen):
            continue
        seen.add(real)
        roots.append(candidate)
    return roots


def _normalise_extensions(extensions: Optional[Iterable[str]]) -> set[str]:
    out: set[str] = set()
    for raw in extensions or []:
        value = str(raw or "").strip().lower().lstrip(".")
        if value:
            out.add(value)
    return out


def _matches(name: str, query: str, extensions: set[str]) -> bool:
    lowered = name.lower()
    if extensions and lowered.rsplit(".", 1)[-1] not in extensions:
        return False
    if not query or query == "*":
        return True
    pattern = query.lower()
    if any(ch in pattern for ch in "*?[]"):
        return fnmatch.fnmatch(lowered, pattern) or fnmatch.fnmatch(lowered, f"*{pattern}")
    return pattern in lowered


def _walk(
    root: Path, query: str, extensions: set[str], since: Optional[float],
    max_depth: int, found: list[dict[str, Any]], budget: list[int],
) -> None:
    stack: list[tuple[Path, int]] = [(root, 0)]
    while stack and budget[0] > 0:
        current, depth = stack.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    budget[0] -= 1
                    if budget[0] <= 0:
                        return
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            if depth + 1 > max_depth or entry.name.lower() in _SKIP_DIRS:
                                continue
                            if entry.name.startswith("."):
                                continue
                            stack.append((Path(entry.path), depth + 1))
                            continue
                        if not entry.is_file(follow_symlinks=False):
                            continue
                        stat = entry.stat()
                    except OSError:
                        continue
                    if since is not None and stat.st_mtime < since:
                        continue
                    if not _matches(entry.name, query, extensions):
                        continue
                    found.append({
                        "path": str(Path(entry.path).resolve()),
                        "name": entry.name,
                        "size_bytes": stat.st_size,
                        "modified": stat.st_mtime,
                    })
        except OSError:
            continue


def locate(
    query: str = "",
    *,
    extensions: Optional[Iterable[str]] = None,
    since_days: Optional[float] = None,
    limit: int = 25,
    folders: Optional[Iterable[str]] = None,
    max_depth: int = 4,
) -> dict[str, Any]:
    """Find files by name fragment/glob under the person's own folders.

    Returns `{"roots": [...], "files": [{path, name, size_bytes, modified}],
    "scanned": n, "truncated": bool}`. `files` is newest first and absolute.
    """
    ext_set = _normalise_extensions(extensions)
    if folders:
        roots = [Path(str(folder)).expanduser() for folder in folders]
        roots = [root for root in roots if root.is_dir()]
    else:
        roots = search_roots()
    since = time.time() - float(since_days) * 86_400 if since_days else None
    found: list[dict[str, Any]] = []
    budget = [_MAX_ENTRIES]
    for root in roots:
        if budget[0] <= 0:
            break
        _walk(root, query, ext_set, since, max_depth, found, budget)
    found.sort(key=lambda item: item["modified"], reverse=True)
    limit = max(1, min(int(limit or 25), 200))
    return {
        "roots": [str(root) for root in roots],
        "files": found[:limit],
        "scanned": _MAX_ENTRIES - max(0, budget[0]),
        "truncated": len(found) > limit or budget[0] <= 0,
    }


__all__ = [
    "OUTPUT_DIR_ENV",
    "exports_dir",
    "known_folder",
    "locate",
    "publish",
    "publish_copy",
    "destination",
    "safe_filename",
    "search_roots",
]
