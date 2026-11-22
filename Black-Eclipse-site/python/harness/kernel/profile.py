"""Profiles and bundles: ordered composition layers with a safe loader.

A profile is ``<dir>/profile.json`` plus a patch file. Its ``bundles`` list names
ordered bundle packages; each bundle declares one patch file (or a list) in its
``plugin.yaml`` under ``bundle.patch``. A missing or unreadable bundle is skipped
with a diagnostic, matching DSH's "keep the remaining composition" behavior;
profile and user-patch errors fail composition because the user asked for them.

Bundle resolution also enforces supply-chain and compatibility rules before any
row is composed: declared platforms, a kernel version range, required bundle
dependencies (including cycle detection), and an optional Ed25519 signature over
the bundle declaration plus its patch digests. A failing bundle is skipped with
a named diagnostic; it never contributes rows.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

from .composition import Composition, Layer, compose
from .errors import CompositionError

PROFILE_FILE = "profile.json"
DEFAULT_PATCH = "cordis.patch.yaml"
BUNDLE_FILES = ("plugin.yaml", "plugin.yml", "bundle.yaml")
REQUIRE_SIGNATURE_ENV = "ECLIPSE_KERNEL_REQUIRE_SIGNATURE"
TRUSTED_KEYS_ENV = "ECLIPSE_KERNEL_TRUSTED_KEYS"


@dataclass
class BundlePolicy:
    """Operator policy for bundle trust and compatibility.

    Defaults are lenient only where existing unsigned bundles must keep working:
    a declared signature is *always* verified, and a requirement flag makes a
    missing signature a skip. Platform/version/cycle/dependency checks always
    run when the bundle declares them.
    """

    platform: str = field(default_factory=lambda: sys.platform)
    host_version: str = ""
    trusted_keys: tuple[bytes, ...] = ()
    require_signature: bool = False


def bundle_policy_from_env(env: Optional[dict[str, str]] = None) -> BundlePolicy:
    source = env if env is not None else os.environ
    raw_keys = str(source.get(TRUSTED_KEYS_ENV) or "")
    keys: list[bytes] = []
    for item in raw_keys.replace(";", ",").split(","):
        value = item.strip()
        if not value:
            continue
        try:
            key = bytes.fromhex(value)
        except ValueError as exc:
            raise CompositionError(
                "KERNEL_BUNDLE_TRUSTED_KEY", f"invalid trusted key {value!r}: {exc}",
                source=TRUSTED_KEYS_ENV,
            ) from exc
        if len(key) != 32:
            raise CompositionError(
                "KERNEL_BUNDLE_TRUSTED_KEY",
                f"trusted key {value!r} must be 32 bytes (Ed25519 public key)", source=TRUSTED_KEYS_ENV,
            )
        keys.append(key)
    required = str(source.get(REQUIRE_SIGNATURE_ENV) or "").strip().lower()
    return BundlePolicy(
        trusted_keys=tuple(keys),
        require_signature=required in ("1", "true", "yes", "on", "required"),
    )


def _host_version() -> str:
    from ..plugins.manifest import ECLIPSE_VERSION  # noqa: PLC0415 - optional dependency at load time

    return ECLIPSE_VERSION


@dataclass
class BundleRef:
    name: str
    root: Optional[Path] = None
    patches: list[Path] = field(default_factory=list)
    error: str = ""
    requires: list[str] = field(default_factory=list)


@dataclass
class Profile:
    name: str
    directory: Path
    bundles: list[BundleRef] = field(default_factory=list)
    patch: Optional[Path] = None
    composition: Optional[Composition] = None


def bundle_signature_payload(declaration: dict[str, Any], patches: Iterable[Path]) -> bytes:
    """Canonical bytes a bundle publisher signs.

    Covers every declared field except ``signature`` and the SHA-256 of each
    patch file, so editing either the declaration or a patch invalidates it.
    """
    body = {key: value for key, value in declaration.items() if key != "signature"}
    files = []
    for path in patches:
        files.append({
            "name": Path(path).name,
            "sha256": hashlib.sha256(Path(path).read_bytes()).hexdigest(),
        })
    canonical = {"bundle": body, "patches": files}
    return json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _verify_signature(declaration: dict[str, Any], patches: list[Path], policy: BundlePolicy) -> str:
    from ..plugins.ed25519 import verify  # noqa: PLC0415

    raw = str(declaration.get("signature") or "").strip()
    if not raw:
        return "missing signature" if policy.require_signature else ""
    if raw.lower().startswith("ed25519:"):
        raw = raw.split(":", 1)[1]
    try:
        signature = bytes.fromhex(raw)
    except ValueError:
        return "signature is not hex"
    if not policy.trusted_keys:
        return "no trusted key is configured to verify this bundle"
    message = bundle_signature_payload(declaration, patches)
    if not any(verify(key, message, signature) for key in policy.trusted_keys):
        return "signature does not verify against any trusted key"
    return ""


def _check_compatibility(declaration: dict[str, Any], policy: BundlePolicy) -> str:
    platforms = declaration.get("platforms")
    if platforms is None:
        platforms = declaration.get("platform")
    if platforms:
        allowed = [str(item) for item in platforms] if isinstance(platforms, list) else [str(platforms)]
        if policy.platform not in allowed:
            return f"platform {policy.platform!r} is not in {allowed}"
    requirement = declaration.get("kernel") or declaration.get("eclipse")
    if requirement:
        from ..plugins import semver  # noqa: PLC0415

        host = policy.host_version or _host_version()
        if isinstance(requirement, dict):
            minimum = requirement.get("min") or requirement.get("minVersion")
            maximum = requirement.get("max") or requirement.get("maxVersion")
            if minimum and semver.compare(host, str(minimum)) < 0:
                return f"requires kernel >= {minimum} (host is {host})"
            if maximum and semver.compare(host, str(maximum)) > 0:
                return f"requires kernel <= {maximum} (host is {host})"
        else:
            expr = str(requirement)
            try:
                ok = semver.satisfies(host, expr)
            except Exception as exc:  # noqa: BLE001 - a bad range is a named skip
                return f"invalid kernel range {expr!r}: {exc}"
            if not ok:
                return f"requires kernel {expr} (host is {host})"
    return ""


def load_profile(
    directory: str | Path,
    *,
    bundle_paths: Iterable[str | Path] = (),
    extra_patches: Iterable[str | Path] = (),
    env: Optional[dict[str, str]] = None,
    refs: Optional[dict[str, Any]] = None,
    policy: Optional[BundlePolicy] = None,
) -> Profile:
    directory = Path(directory).expanduser()
    manifest_path = directory / PROFILE_FILE
    if not manifest_path.is_file():
        raise CompositionError(
            "KERNEL_PROFILE_MISSING", f"no profile at {directory}",
            source=str(directory),
        )
    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 - malformed profile is a named rejection
        raise CompositionError(
            "KERNEL_PROFILE_INVALID", f"cannot read {manifest_path.name}: {exc}", source=str(manifest_path),
        ) from exc
    if not isinstance(raw, dict):
        raise CompositionError("KERNEL_PROFILE_INVALID", "profile.json must be an object", source=str(manifest_path))

    name = str(raw.get("name") or directory.name)
    patch_name = str(raw.get("patch") or DEFAULT_PATCH)
    patch_path = (directory / patch_name).resolve() if patch_name else None
    bundle_names = raw.get("bundles") or []
    if not isinstance(bundle_names, list):
        raise CompositionError(
            "KERNEL_PROFILE_INVALID", "profile.bundles must be a list", source=str(manifest_path),
        )

    policy = policy or bundle_policy_from_env(env)
    roots = [Path(root).expanduser() for root in bundle_paths]
    roots.append(directory)
    bundles = [_resolve_bundle(str(item), roots, policy) for item in bundle_names]
    _check_dependencies(bundles)
    layers: list[Layer] = []
    for bundle in bundles:
        if bundle.error:
            continue
        for path in bundle.patches:
            layers.append(Layer(source=str(path), patch=_read_patch(path)))
    if patch_path is not None and patch_path.is_file():
        layers.append(Layer(source=str(patch_path), patch=_read_patch(patch_path)))
    elif patch_path is not None and patch_path.exists() is False:
        # A profile may legitimately ship without a patch file.
        pass
    for extra in extra_patches:
        path = Path(extra).expanduser()
        if not path.is_file():
            raise CompositionError("KERNEL_PATCH_MISSING", f"patch not found: {path}", source=str(path))
        layers.append(Layer(source=str(path), patch=_read_patch(path)))

    composition = compose(layers, env=env, refs=refs)
    for bundle in bundles:
        if bundle.error:
            composition.diagnostics.append({
                "code": "KERNEL_BUNDLE_SKIPPED",
                "bundle": bundle.name,
                "message": bundle.error,
            })
    return Profile(
        name=name,
        directory=directory,
        bundles=bundles,
        patch=patch_path if patch_path and patch_path.is_file() else None,
        composition=composition,
    )


def _check_dependencies(bundles: list[BundleRef]) -> None:
    """Skip bundles with unknown requirements and every bundle in a cycle."""
    present = {bundle.name for bundle in bundles if not bundle.error}
    for bundle in bundles:
        if bundle.error:
            continue
        missing = [item for item in bundle.requires if item not in present]
        if missing:
            bundle.error = f"requires missing bundle(s): {', '.join(missing)}"
            bundle.patches = []
    graph = {bundle.name: [item for item in bundle.requires if item in present] for bundle in bundles if not bundle.error}
    for name, path in _find_cycles(graph).items():
        bundle = next(item for item in bundles if item.name == name)
        bundle.error = f"bundle dependency cycle: {' -> '.join(path)}"
        bundle.patches = []


def _find_cycles(graph: dict[str, list[str]]) -> dict[str, list[str]]:
    cycles: dict[str, list[str]] = {}
    visiting: list[str] = []
    done: set[str] = set()

    def walk(node: str) -> None:
        if node in visiting:
            cycle = visiting[visiting.index(node):] + [node]
            for member in cycle[:-1]:
                cycles.setdefault(member, cycle)
            return
        if node in done:
            return
        visiting.append(node)
        for child in graph.get(node, []):
            walk(child)
        visiting.pop()
        done.add(node)

    for node in graph:
        walk(node)
    return cycles


def _resolve_bundle(name: str, roots: list[Path], policy: BundlePolicy) -> BundleRef:
    for root in roots:
        directory = root / name
        for filename in BUNDLE_FILES:
            manifest = directory / filename
            if manifest.is_file():
                try:
                    data = _read_yaml(manifest)
                except Exception as exc:  # noqa: BLE001
                    return BundleRef(name=name, root=directory, error=f"unreadable bundle manifest: {exc}")
                bundle = data.get("bundle") if isinstance(data, dict) else None
                if not isinstance(bundle, dict):
                    return BundleRef(name=name, root=directory, error="bundle declares no bundle section")
                patch = bundle.get("patch")
                candidates = patch if isinstance(patch, list) else [patch] if patch else []
                patches = [(directory / str(item)).resolve() for item in candidates]
                requires = bundle.get("requires") or []
                if not isinstance(requires, list):
                    return BundleRef(name=name, root=directory, error="bundle.requires must be a list")
                if not patches:
                    return BundleRef(name=name, root=directory, error="bundle declares no patch")
                missing = [str(path) for path in patches if not path.is_file()]
                if missing:
                    return BundleRef(name=name, root=directory, error=f"missing patch: {', '.join(missing)}")
                refusal = _check_compatibility(bundle, policy) or _verify_signature(bundle, patches, policy)
                if refusal:
                    return BundleRef(
                        name=name, root=directory, patches=[], error=refusal, requires=[str(x) for x in requires],
                    )
                return BundleRef(
                    name=name, root=directory, patches=patches, requires=[str(x) for x in requires],
                )
        direct = root / f"{name}.patch.yaml"
        if direct.is_file():
            return BundleRef(name=name, root=root, patches=[direct.resolve()])
    return BundleRef(name=name, error=f"bundle {name!r} not found in {', '.join(str(r) for r in roots)}")


def _read_patch(path: Path) -> list[dict[str, Any]]:
    data = _read_yaml(path)
    if data is None:
        return []
    if not isinstance(data, list):
        raise CompositionError(
            "KERNEL_COMPOSITION_SHAPE", f"{path.name} must contain a list of patch operations",
            source=str(path),
        )
    return data


def _read_yaml(path: Path) -> Any:
    import yaml  # noqa: PLC0415 - imported only when a profile is loaded

    return yaml.safe_load(path.read_text(encoding="utf-8"))
