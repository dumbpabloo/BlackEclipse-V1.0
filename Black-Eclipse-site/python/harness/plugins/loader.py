"""Discover, read and instantiate plugins.

A plugin directory contains a manifest (`plugin.yaml`/`plugin.yml`/`plugin.json`)
and the runtime entrypoint named by `runtime.entrypoint`. The loader reads the
manifest, validates it, computes a checksum over the package, and instantiates
the runtime object. Importing plugin code happens here — the manager decides
WHEN to call it (on enable), never the loader.

Built-in reference plugins live under `bundled/`; installed plugins under
`.black-eclipse/plugins/<id>/`.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any, Optional

from .errors import ManifestError, PluginError
from .manifest import PluginManifest, parse_manifest

MANIFEST_FILES = ("plugin.yaml", "plugin.yml", "plugin.json")
# Files never counted toward the plugin's content checksum (transient artifacts).
_CHECKSUM_SKIP = {"__pycache__", ".git", ".pytest_cache", ".DS_Store", "data", "plugin.sig"}


class LoadError(PluginError):
    def __init__(self, code: str, message: str, **kwargs: Any) -> None:
        super().__init__(code, message, **kwargs)


def find_manifest_file(root: Path) -> Optional[Path]:
    for name in MANIFEST_FILES:
        candidate = root / name
        if candidate.is_file():
            return candidate
    return None


def read_manifest(root: Path) -> tuple[PluginManifest, dict[str, Any]]:
    path = find_manifest_file(root)
    if path is None:
        raise LoadError("PLUGIN_MANIFEST_MISSING", f"no manifest in {root}")
    try:
        text = path.read_text(encoding="utf-8")
        if path.suffix == ".json":
            raw = json.loads(text)
        else:
            import yaml  # noqa: PLC0415

            raw = yaml.safe_load(text)
    except Exception as exc:  # noqa: BLE001 - a malformed manifest is a named rejection
        raise ManifestError(f"could not parse {path.name}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ManifestError(f"{path.name} must contain a mapping at the top level")
    try:
        manifest = parse_manifest(raw)
    except Exception as exc:  # noqa: BLE001 - pydantic ValidationError is a rejection
        raise ManifestError(f"manifest validation failed: {_first_error(exc)}") from exc
    return manifest, raw


def _first_error(exc: Exception) -> str:
    errors = getattr(exc, "errors", None)
    if callable(errors):
        try:
            errs = errors()
            if errs:
                loc = ".".join(str(p) for p in errs[0].get("loc", ()))
                return f"{loc}: {errs[0].get('msg')}"
        except Exception:  # noqa: BLE001
            pass
    return str(exc)


def checksum(root: Path) -> str:
    """A stable SHA-256 over the plugin package contents (order-independent)."""
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root)
        if any(part in _CHECKSUM_SKIP for part in rel.parts):
            continue
        if not path.is_file():
            continue
        digest.update(str(rel).replace("\\", "/").encode("utf-8"))
        digest.update(b"\0")
        try:
            digest.update(path.read_bytes())
        except Exception:  # noqa: BLE001 - an unreadable file is simply not hashed
            continue
        digest.update(b"\0")
    return digest.hexdigest()


def instantiate(manifest: PluginManifest, root: Path) -> Any:
    """Import and construct the plugin runtime object named by the manifest."""
    entrypoint = manifest.runtime.entrypoint or ""
    module_part, _, attr = entrypoint.partition(":")
    attr = attr or "plugin"
    file_path: Optional[Path] = None
    if module_part.endswith(".py"):
        file_path = (root / module_part).resolve()
        if not file_path.is_file():
            raise LoadError("PLUGIN_ENTRYPOINT_MISSING", f"entrypoint file not found: {module_part}")
    else:
        rel = module_part.replace(".", "/") + ".py"
        if (root / rel).is_file():
            file_path = (root / rel).resolve()
        elif (root / module_part).is_dir() and (root / module_part / "__init__.py").is_file():
            file_path = (root / module_part / "__init__.py").resolve()
    if file_path is None:
        raise LoadError("PLUGIN_ENTRYPOINT_MISSING", f"could not resolve entrypoint module {module_part!r}")

    try:
        module = _import_from_path(manifest.id, file_path, name=module_part if not module_part.endswith(".py") else file_path.stem)
    except Exception as exc:  # noqa: BLE001 - an import failure is a load failure
        raise LoadError("PLUGIN_IMPORT_FAILED", f"importing entrypoint failed: {type(exc).__name__}: {exc}") from exc

    target = getattr(module, attr, None)
    if target is None:
        raise LoadError("PLUGIN_ENTRYPOINT_MISSING", f"attribute {attr!r} not found in {module_part!r}")
    try:
        return target() if isinstance(target, type) else target
    except Exception as exc:  # noqa: BLE001
        raise LoadError("PLUGIN_CONSTRUCT_FAILED", f"constructing plugin failed: {exc}") from exc


def _import_from_path(plugin_id: str, path: Path, *, name: str) -> Any:
    safe = "".join(c if c.isalnum() else "_" for c in plugin_id)
    mod_name = f"eclipse_plugin_{safe}_{abs(hash(str(path))) & 0xFFFF:x}"
    if mod_name in sys.modules:
        return sys.modules[mod_name]
    # Support packages that import sibling modules: add the plugin root to sys.path.
    root_str = str(path.parent)
    added = False
    if root_str not in sys.path:
        sys.path.insert(0, root_str)
        added = True
    try:
        spec = importlib.util.spec_from_file_location(mod_name, str(path))
        if spec is None or spec.loader is None:
            raise LoadError("PLUGIN_IMPORT_FAILED", f"cannot load {path}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[mod_name] = module
        try:
            spec.loader.exec_module(module)
        except BaseException:
            # A half-imported module must not poison the cache: the next load
            # (after the source is fixed) has to re-execute from scratch.
            sys.modules.pop(mod_name, None)
            raise
        return module
    finally:
        if added:
            try:
                sys.path.remove(root_str)
            except ValueError:
                pass


def module_names_under(root: Path) -> list[str]:
    """Imported module names whose file lives under ``root`` (non-destructive)."""
    try:
        resolved = Path(root).resolve()
    except OSError:
        return []
    found: list[str] = []
    for name, module in list(sys.modules.items()):
        file_path = getattr(module, "__file__", None)
        if not file_path:
            continue
        try:
            path = Path(file_path).resolve()
        except (OSError, ValueError):
            continue
        if path == resolved or resolved in path.parents:
            found.append(name)
    return sorted(found)


def evict_modules(root: Path) -> list[str]:
    """Remove every imported module whose file lives under ``root``.

    This is the code half of a hot reload: the next ``instantiate`` re-executes
    the entrypoint *and* any sibling module the plugin imported. Returns the
    evicted module names (for evidence/tests).
    """
    removed = module_names_under(root)
    for name in removed:
        sys.modules.pop(name, None)
    importlib.invalidate_caches()
    return removed


def discover(root: Path) -> list[Path]:
    """Directories under `root` that contain a manifest."""
    if not root.is_dir():
        return []
    found = []
    for child in sorted(root.iterdir()):
        if child.is_dir() and find_manifest_file(child) is not None:
            found.append(child)
    return found


__all__ = [
    "read_manifest",
    "instantiate",
    "checksum",
    "discover",
    "evict_modules",
    "module_names_under",
    "find_manifest_file",
    "LoadError",
    "MANIFEST_FILES",
]
